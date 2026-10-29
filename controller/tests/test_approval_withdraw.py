"""What an operator's withdrawing approve is told, and what the trail and the witness chain then say.

An operator rejects an approval its action has claimed; the reject waits on the
claim, and an approve before the action begins withdraws it. The answer said
"the action may start" while the action was being refused for another reason,
and "the agent may retry with this approval_id" for an approval that had expired
while it waited, whose retry was then refused.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.action_errors import BrowserActionError
from app.approvals import given_back_notes, held_for_execution
from tests.test_approval_give_back_window import WAIT_SECONDS, ClaimWindowBase
from tests.test_approval_spend import OFF_LIST_URL, PAST, SESSION


class WithdrawBase(ClaimWindowBase):
    def withdraw_once_the_click_is_receipted(self, approval_id: str) -> list[dict[str, Any]]:
        """Approve ``approval_id`` once the refused click's witness receipt is written: the answers."""
        answers: list[dict[str, Any]] = []
        receipt = self.manager._record_witness_receipt

        async def withdraw_after(session: Any, **event: Any) -> Any:
            written = await receipt(session, **event)
            if event.get("event_type") == "browser_action" and not answers:
                answers.append(await self.manager.approve(approval_id, comment="go ahead after all"))
            return written

        self.manager._record_witness_receipt = withdraw_after  # type: ignore[method-assign]
        return answers


class WithdrawAnswerTests(WithdrawBase):
    async def test_a_withdraw_while_a_policy_block_is_recorded_says_the_action_was_refused(self) -> None:
        # The page left ALLOWED_HOSTS: the click is refused before it begins, and the refusal was recorded
        # on the hold only once the block's snapshot and trail were written. An approve meanwhile was told
        # the action may start.
        _, approval_id, go, task = await self.claimed_and_waiting()
        await self.manager.reject(approval_id, comment="hold on")  # deferred on the claim
        pipeline = self.manager.action_pipeline
        snapshot = pipeline._failure_snapshot
        answers: list[dict[str, Any]] = []

        async def withdraw_during(context: Any, *, label: str) -> Any:
            if not answers:
                answers.append(await self.manager.approve(approval_id, comment="go ahead after all"))
            return await snapshot(context, label=label)

        pipeline._failure_snapshot = withdraw_during  # type: ignore[method-assign]
        self.page.url = OFF_LIST_URL
        go.set()
        with self.assertRaises(BrowserActionError) as refused:
            await asyncio.wait_for(task, WAIT_SECONDS)

        self.assertEqual(refused.exception.code, "browser_action_blocked")
        self.assertIn("was already refused before it began", answers[0]["message"])
        self.assertNotIn("may start", answers[0]["message"])
        self.assertEqual((await self.status(approval_id), self.page.clicks), ("approved", 0))

    async def test_a_withdraw_of_an_approval_that_expired_while_its_action_waited_says_so(self) -> None:
        decision, approval_id, go, task = await self.claimed_and_waiting()
        store = self.manager.approvals
        record = await store.get(approval_id)
        record.approved_expires_at = PAST  # claimed in time, expired while it waits
        await store._persist(record)
        await self.manager.reject(approval_id, comment="hold on")  # deferred on the claim
        answers = self.withdraw_once_the_click_is_receipted(approval_id)

        go.set()  # the rejection refuses the click before it begins
        with self.assertRaises(BrowserActionError) as refused:
            await asyncio.wait_for(task, WAIT_SECONDS)

        message = answers[0]["message"]
        self.assertIn("has expired", message)
        self.assertIn("ask for a new approval", message)
        self.assertNotIn("may retry", message)
        self.assertEqual(given_back_notes(refused.exception)["approval_given_back"]["status"], "expired")
        with self.assertRaisesRegex(PermissionError, "has expired"):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)


class WithdrawReceiptTests(WithdrawBase):
    async def chain(self) -> list[tuple[str, str, str, str | None]]:
        """The session's witness chain, oldest first: (event, status, action, the approval's status)."""
        receipts = reversed(await self.manager.list_witness_receipts(SESSION))
        return [
            (item["event_type"], item["status"], item["action"], (item.get("approval") or {}).get("status"))
            for item in receipts
            if item["event_type"] in ("approval", "browser_action")
        ]

    async def test_a_withdraw_after_the_rejection_stopped_the_action_is_in_the_witness_chain(self) -> None:
        # F3: the chain read the click blocked by a rejected approval, then the same approval clicking,
        # with nothing between: the operator's approve that withdrew the rejection left no receipt.
        decision, approval_id, go, task = await self.claimed_and_waiting()
        await self.manager.reject(approval_id, comment="hold on")  # deferred on the claim
        answers = self.withdraw_once_the_click_is_receipted(approval_id)
        go.set()  # the rejection refuses the click before it begins
        with self.assertRaises(BrowserActionError):
            await asyncio.wait_for(task, WAIT_SECONDS)
        self.assertIn("was already refused before it began", answers[0]["message"])

        self.assertEqual(await self.retry(decision, approval_id), "ran")

        self.assertEqual(
            await self.chain(),
            [
                ("approval", "pending", "approval_requested", "pending"),
                ("approval", "approved", "approve", "approved"),
                ("browser_action", "blocked", "click", "rejected"),
                ("approval", "approved", "approve", "approved"),
                ("browser_action", "ok", "click", "executed"),
            ],
        )


class WithdrawBoundaryTests(WithdrawBase):
    """Call A's boundary: what decides an ending is read at the give-back's write, under the store lock."""

    async def test_a_withdraw_during_the_give_back_of_an_unrecorded_error_leaves_it_spent(self) -> None:
        # F4: an error nothing recorded leaves the approval spent unless a reject waits; a reject made this
        # one give back, and an approve that withdrew it before the write left nothing to give back for,
        # yet the record came back approved, and the operator was told so.
        _, approval_id = await self.approved_click()
        store = self.manager.approvals
        give_back = store.give_back
        answers: list[dict[str, Any]] = []

        async def withdraw_first(hold: Any) -> Any:
            answers.append(await self.manager.approve(approval_id, comment="go ahead after all"))
            return await give_back(hold)

        store.give_back = withdraw_first  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            async with held_for_execution(store, approval_id, may_give_back=True):
                await self.manager.reject(approval_id, comment="no")  # deferred on the claim
                raise RuntimeError("nothing recorded this: an error, not a refusal")

        self.assertEqual(await self.status(approval_id), "executed")
        self.assertIn("stays spent", answers[0]["message"])
        self.assertNotIn("goes back to approved", answers[0]["message"])
        given_back = await self.manager.list_audit_events(limit=50, event_type="approval_given_back")
        self.assertEqual([event for event in given_back if event["approval_id"] == approval_id], [])
