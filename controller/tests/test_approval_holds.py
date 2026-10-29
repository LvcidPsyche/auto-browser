"""The hold an execution takes on its approval: spent at claim, given back only on some endings before any effect.

held_for_execution is how every path runs an approved action. Its claim spends
the approval (persisted, so a restart cannot reuse it), and the layers inside
the execution see the hold and do not claim again. The hold is one object
shared with every task the execution runs in, and releasing it revokes it: a
task copied from the holder's context used to keep the approval id after the
holder had let go, and passed as part of an execution that had ended.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path

from app.approvals import ApprovalRefusal, ApprovalStore, effect_started, held_for_execution, refused_before_effect
from app.models import BrowserActionDecision

SESSION = "session-1"
# Long enough for a loaded machine; what the tests wait on is an event, not a duration.
WAIT_SECONDS = 30
ACTION = BrowserActionDecision(action="click", reason="pay", selector="#pay", risk_category="payment")
OTHER_ACTION = BrowserActionDecision(action="click", reason="save", selector="#save", risk_category="write")


class HoldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = ApprovalStore(self.root)
        await self.store.startup()
        approval = await self.store.create_or_reuse_pending(
            session_id=SESSION, kind="payment", reason="r", action=ACTION
        )
        await self.store.approve(approval.id)
        self.approval_id = approval.id

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def status(self) -> str:
        return (await self.store.get(self.approval_id)).status

    async def reset_to_approved(self) -> None:
        approval = await self.store.get(self.approval_id)
        await self.store.file_store.upsert(approval.model_copy(update={"status": "approved", "executed_at": None}))

    async def check(self) -> None:
        await self.store.require_approved(
            approval_id=self.approval_id, session_id=SESSION, kind="payment", action=ACTION
        )

    async def test_the_claim_spends_the_approval_before_the_action_runs(self) -> None:
        async with held_for_execution(self.store, self.approval_id):
            restarted = ApprovalStore(self.root)
            seen = await restarted.get(self.approval_id)

        self.assertEqual(seen.status, "executed")
        self.assertIsNotNone(seen.executed_at)

    async def test_a_refusal_before_any_effect_gives_the_approval_back(self) -> None:
        refusal = PermissionError("refused before the operation")

        with self.assertRaises(PermissionError):
            async with held_for_execution(self.store, self.approval_id, may_give_back=True):
                refused_before_effect(refusal)
                raise refusal

        approval = await self.store.get(self.approval_id)
        self.assertEqual((approval.status, approval.executed_at), ("approved", None))

    async def test_a_hold_that_may_not_give_back_keeps_even_that_refusal_spent(self) -> None:
        # The default: a block that is not exactly one action call (a governed tool's handler).
        refusal = PermissionError("refused before the operation")

        with self.assertRaises(PermissionError):
            async with held_for_execution(self.store, self.approval_id):
                refused_before_effect(refusal)
                raise refusal

        self.assertEqual(await self.status(), "executed")

    async def test_everything_else_keeps_it_spent(self) -> None:
        # With no operator's reject waiting on the hold: one that waits applies however the block ends (below).
        refusal = PermissionError("refused after the change")
        other = RuntimeError("then something else")

        async def refused_after_effect() -> None:
            effect_started()
            refused_before_effect(refusal)
            raise refusal

        async def other_error() -> None:
            refused_before_effect(PermissionError("an earlier refusal, caught"))
            raise other

        endings = {refused_after_effect: refusal, other_error: other}
        for ending, raised in endings.items():
            with self.subTest(ending=ending.__name__):
                await self.reset_to_approved()

                with self.assertRaises(type(raised)) as ended:
                    async with held_for_execution(self.store, self.approval_id, may_give_back=True):
                        await ending()

                # The very exception the block raised goes on.
                self.assertIs(ended.exception, raised)
                self.assertEqual(await self.status(), "executed")

    async def test_a_cancellation_before_any_effect_gives_the_approval_back(self) -> None:
        # The caller gave up (a disconnect, a timeout) before the action began: nothing ran.
        cancellation = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError) as ended:
            async with held_for_execution(self.store, self.approval_id, may_give_back=True):
                raise cancellation

        # The very cancellation goes on: it stays the caller's own.
        self.assertIs(ended.exception, cancellation)
        approval = await self.store.get(self.approval_id)
        self.assertEqual((approval.status, approval.executed_at), ("approved", None))

    async def test_a_cancellation_once_the_action_began_or_outside_a_give_back_block_stays_spent(self) -> None:
        async def after_the_effect() -> None:
            effect_started()
            raise asyncio.CancelledError

        async def before_it() -> None:
            raise asyncio.CancelledError

        for ending, may_give_back in ((after_the_effect, True), (before_it, False)):
            with self.subTest(ending=ending.__name__, may_give_back=may_give_back):
                await self.reset_to_approved()

                with self.assertRaises(asyncio.CancelledError):
                    async with held_for_execution(self.store, self.approval_id, may_give_back=may_give_back):
                        await ending()

                self.assertEqual(await self.status(), "executed")

    async def test_any_ending_before_the_effect_applies_a_waiting_rejection(self) -> None:
        # The deferred reject's answer promised it: the approval then becomes rejected. Nothing ran.
        async def other_error() -> None:
            raise RuntimeError("the staged file went away")

        async def cancelled() -> None:
            raise asyncio.CancelledError

        async def returned() -> None:
            return None

        for number, ending in enumerate((other_error, cancelled, returned)):
            with self.subTest(ending=ending.__name__):
                action = OTHER_ACTION.model_copy(update={"selector": f"#save-{number}"})
                approval = await self.store.create_or_reuse_pending(
                    session_id=SESSION, kind="write", reason="r", action=action
                )
                await self.store.approve(approval.id)

                with contextlib.suppress(RuntimeError, asyncio.CancelledError):
                    async with held_for_execution(self.store, approval.id, may_give_back=True):
                        await self.store.reject(approval.id, comment="no")  # deferred on the claim
                        await ending()

                rejected = await self.store.get(approval.id)
                self.assertEqual(
                    (rejected.status, rejected.decision_comment, rejected.executed_at), ("rejected", "no", None)
                )

    async def test_a_block_that_returns_keeps_it_spent(self) -> None:
        # Nothing refused, cancelled or rejected: the action ran as far as it was going to.
        async with held_for_execution(self.store, self.approval_id, may_give_back=True):
            pass

        self.assertEqual(await self.status(), "executed")

    async def test_only_the_very_refusal_recorded_gives_the_approval_back(self) -> None:
        # The same class and message, another object: not the refusal the pipeline recorded.
        recorded = PermissionError("refused before the operation")

        with self.assertRaises(PermissionError):
            async with held_for_execution(self.store, self.approval_id, may_give_back=True):
                refused_before_effect(recorded)
                raise PermissionError("refused before the operation")

        self.assertEqual(await self.status(), "executed")

    async def test_the_recorded_refusal_raised_after_the_effect_keeps_it_spent(self) -> None:
        # Pins the give-back's own check of the effect, apart from refused_before_effect's.
        refusal = PermissionError("refused before the operation")

        with self.assertRaises(PermissionError):
            async with held_for_execution(self.store, self.approval_id, may_give_back=True):
                refused_before_effect(refusal)
                effect_started()
                raise refusal

        self.assertEqual(await self.status(), "executed")

    async def test_a_refusal_after_the_effect_is_not_recorded(self) -> None:
        # Pins refused_before_effect's own check of the effect, apart from the give-back's.
        hold = await self.store.claim_execution(self.approval_id)
        try:
            effect_started()
            refused_before_effect(PermissionError("refused after the change"))
            self.assertIsNone(hold.refusal)
        finally:
            self.store.release_execution(hold, spent=True)

    async def test_a_released_claim_is_no_longer_registered(self) -> None:
        # Left registered, it would defer every later reject of the spent approval: 200 "deferred", for good.
        with self.assertRaises(RuntimeError):
            async with held_for_execution(self.store, self.approval_id, may_give_back=True):
                raise RuntimeError("crashed before any effect")

        self.assertNotIn(self.approval_id, self.store._claims)
        with self.assertRaisesRegex(PermissionError, "has already been spent: its action started"):
            await self.store.reject(self.approval_id, comment="too late")

    async def test_a_rejection_a_give_back_applies_ends_the_approval_and_drops_its_text(self) -> None:
        typed = BrowserActionDecision(action="type", reason="r", selector="#pw", text="hunter2", sensitive=True)
        approval = await self.store.create_or_reuse_pending(
            session_id=SESSION, kind="account_change", reason="r", action=typed
        )
        await self.store.approve(approval.id)
        refusal = PermissionError("refused before the operation")

        with self.assertRaises(PermissionError):
            async with held_for_execution(self.store, approval.id, may_give_back=True):
                await self.store.reject(approval.id, comment="no")  # deferred: the claim may still give it back
                refused_before_effect(refusal)
                raise refusal

        rejected = await self.store.get(approval.id)
        self.assertEqual((rejected.status, rejected.approved_expires_at), ("rejected", None))
        self.assertNotIn(approval.id, self.store._sensitive_texts)

    async def test_an_inner_hold_that_refuses_marks_no_hold_as_begun(self) -> None:
        # effect_started checks every hold before it marks any, so the outer approval can still be given back.
        inner = await self.store.create_or_reuse_pending(
            session_id=SESSION, kind="write", reason="r", action=OTHER_ACTION
        )
        await self.store.approve(inner.id)

        async with held_for_execution(self.store, self.approval_id, may_give_back=True):
            outer = self.store._claims[self.approval_id]
            async with held_for_execution(self.store, inner.id, may_give_back=True):
                await self.store.reject(inner.id, comment="no")  # deferred on the inner claim
                with self.assertRaises(ApprovalRefusal) as refused:
                    effect_started()
                self.assertEqual(refused.exception.code, "approval_rejected")
                self.assertFalse(outer.effect)

    async def test_a_child_task_left_behind_cannot_use_the_hold_once_it_is_released(self) -> None:
        go = asyncio.Event()
        ran: list[bool] = []

        async def straggler() -> None:
            await go.wait()
            async with held_for_execution(self.store, self.approval_id):
                ran.append(True)

        async with held_for_execution(self.store, self.approval_id):
            child = asyncio.create_task(straggler())
            await asyncio.sleep(0)
        go.set()

        with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
            await child
        with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
            await self.check()
        self.assertEqual(ran, [])

    async def test_children_of_the_holder_share_its_claim(self) -> None:
        async def inner() -> str:
            async with held_for_execution(self.store, self.approval_id):
                await self.check()
            return "ran"

        async with held_for_execution(self.store, self.approval_id):
            spent_at = (await self.store.get(self.approval_id)).executed_at
            concurrent = await asyncio.gather(inner(), asyncio.create_task(inner()), asyncio.create_task(inner()))
            waited = await asyncio.wait_for(inner(), timeout=WAIT_SECONDS)

        self.assertEqual((concurrent, waited), (["ran", "ran", "ran"], "ran"))
        approval = await self.store.get(self.approval_id)
        self.assertEqual((approval.status, approval.executed_at), ("executed", spent_at))

    async def test_a_layer_inside_the_holder_does_not_give_the_approval_back(self) -> None:
        refusal = PermissionError("refused before the operation")

        async with held_for_execution(self.store, self.approval_id):
            with self.assertRaises(PermissionError):
                async with held_for_execution(self.store, self.approval_id, may_give_back=True):
                    refused_before_effect(refusal)
                    raise refusal
            self.assertEqual(await self.status(), "executed")
            effect_started()

        self.assertEqual(await self.status(), "executed")


if __name__ == "__main__":
    unittest.main()
