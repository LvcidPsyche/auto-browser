"""An operator's decision that lands while a refused action's approval is being given back.

The give-back used to drop the hold from the store's live claims before it wrote
the record back. A reject that read the record in between found no claim and was
refused "has already been spent: its action started", the record then came back
approved, and the agent's retry ran the action the operator had rejected. A
withdrawing approve in the same moment was refused the same way. The hold now
stays a live claim until it is released, so a decision in that moment is applied
by the give-back. A cancellation in that moment waits for the write to land, so the
record and the audit agree.
"""

from __future__ import annotations

import asyncio
import threading
import unittest
from typing import Any

from app.action_errors import BrowserActionError
from app.approval_execution import ApprovalRefusal, _run_through, effect_started, refused_before_effect
from app.approvals import held_for_execution
from app.models import BrowserActionDecision
from tests.test_approval_spend import OFF_LIST_URL, SESSION, START_URL, SpendBase

# Long enough for a loaded machine; what the tests wait on is an event, not a duration.
WAIT_SECONDS = 30


class ClaimWindowBase(SpendBase):
    async def claimed_and_waiting(self) -> tuple[Any, str, asyncio.Event, asyncio.Task]:
        """An approved click, claimed and waiting in the pipeline until ``go`` is set."""
        decision, approval_id = await self.approved_click()
        reached, go = asyncio.Event(), asyncio.Event()

        async def snapshot(*_: Any, **__: Any) -> dict[str, Any]:
            if not go.is_set():
                reached.set()
                await go.wait()
            return {"url": self.page.url}

        self.manager._light_snapshot = snapshot  # type: ignore[method-assign]
        task = asyncio.create_task(self.manager.execute_decision(SESSION, decision, approval_id=approval_id))
        await asyncio.wait_for(reached.wait(), WAIT_SECONDS)
        return decision, approval_id, go, task

    def pause_decision_until_the_give_back_starts(self, task_name: str) -> asyncio.Event:
        """Hold the named task's record read, made under the store lock, until a give-back has begun.

        Returns the event set once that read is paused: the decision then holds the
        lock, and the give-back that starts meanwhile waits on it.
        """
        store = self.manager.approvals
        paused, giving_back = asyncio.Event(), asyncio.Event()
        get, give_back = store.get, store.give_back

        async def paused_get(approval_id: str) -> Any:
            task = asyncio.current_task()
            if task is not None and task.get_name() == task_name and store._lock.locked() and not paused.is_set():
                paused.set()
                await asyncio.wait_for(giving_back.wait(), WAIT_SECONDS)
            return await get(approval_id)

        async def marked_give_back(hold: Any) -> Any:
            giving_back.set()
            return await give_back(hold)

        store.get = paused_get  # type: ignore[method-assign]
        store.give_back = marked_give_back  # type: ignore[method-assign]
        return paused

    async def retry(self, decision: Any, approval_id: str) -> str:
        """The agent's retry with the same approval_id, on a page the action may use: what it was told."""
        self.page.url = START_URL
        try:
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        except PermissionError as exc:
            return f"refused: {exc}"
        return "ran"


class GiveBackWindowTests(ClaimWindowBase):
    async def test_a_reject_while_the_approval_is_given_back_is_applied(self) -> None:
        decision, approval_id, go, task = await self.claimed_and_waiting()
        paused = self.pause_decision_until_the_give_back_starts("operator-reject")
        reject = asyncio.create_task(self.manager.reject(approval_id, comment="do not pay"), name="operator-reject")
        await asyncio.wait_for(paused.wait(), WAIT_SECONDS)

        self.page.url = OFF_LIST_URL  # the click is refused before it begins, and the give-back starts
        go.set()
        with self.assertRaises(BrowserActionError) as refused:
            await asyncio.wait_for(task, WAIT_SECONDS)
        answer = await asyncio.wait_for(reject, WAIT_SECONDS)

        self.assertIn("the rejection stops the action unless it has begun", answer["message"])
        self.assertEqual(refused.exception.payload["approval_given_back"]["status"], "rejected")
        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual((approval.status, approval.decision_comment), ("rejected", "do not pay"))
        self.assertIn("is not approved", await self.retry(decision, approval_id))
        self.assertEqual(self.page.clicks, 0)

    async def test_a_withdrawing_approve_while_the_approval_is_given_back_is_applied(self) -> None:
        decision, approval_id, go, task = await self.claimed_and_waiting()
        await self.manager.reject(approval_id, comment="wait")  # deferred on the claim
        paused = self.pause_decision_until_the_give_back_starts("operator-approve")
        approve = asyncio.create_task(
            self.manager.approve(approval_id, comment="go ahead after all"), name="operator-approve"
        )
        await asyncio.wait_for(paused.wait(), WAIT_SECONDS)

        go.set()  # the deferred rejection refuses the click before it begins, and the give-back starts
        with self.assertRaises(BrowserActionError):
            await asyncio.wait_for(task, WAIT_SECONDS)
        await asyncio.wait_for(approve, WAIT_SECONDS)

        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual(approval.status, "approved")  # as the operator last decided
        self.assertEqual(self.page.clicks, 0)


class ClaimMessageTests(ClaimWindowBase):
    """What a decision or a second execution is told about an approval its action holds."""

    async def test_a_withdraw_after_the_rejection_stopped_the_action_says_it_was_already_refused(self) -> None:
        decision, approval_id, go, task = await self.claimed_and_waiting()
        await self.manager.reject(approval_id, comment="wait")  # deferred on the claim
        answers: list[dict[str, Any]] = []
        append = self.manager.audit.append

        async def withdraw_once_refused(**event: Any) -> Any:
            # The action's terminal entry: the rejection has refused it, and the hold is still live.
            if event.get("event_type") == "browser_action" and not answers:
                answers.append(await self.manager.approve(approval_id, comment="go ahead after all"))
            return await append(**event)

        self.manager.audit.append = withdraw_once_refused  # type: ignore[method-assign]
        go.set()
        with self.assertRaises(BrowserActionError) as refused:
            await asyncio.wait_for(task, WAIT_SECONDS)

        self.assertIn("was already refused before it began", answers[0]["message"])
        self.assertNotIn("may start", answers[0]["message"])
        self.assertEqual(refused.exception.payload["approval_given_back"]["status"], "approved")
        self.assertEqual((await self.status(approval_id), self.page.clicks), ("approved", 0))

    async def test_a_withdraw_before_the_refusal_is_recorded_still_says_it_was_already_refused(self) -> None:
        # An upload by element id calls effect_started in its before_transfer hook, inside cleanup that awaits
        # (the handle's disposal, the marker's removal) before the pipeline records the refusal: an approve in
        # that moment has only the hold's own mark to read that the rejection stopped the action.
        _, approval_id = await self.approved_click()
        answers: list[dict[str, Any]] = []

        with self.assertRaises(ApprovalRefusal):
            async with held_for_execution(self.manager.approvals, approval_id, may_give_back=True):
                await self.manager.reject(approval_id, comment="wait")  # deferred on the claim
                try:
                    effect_started()
                except ApprovalRefusal as exc:
                    answers.append(await self.manager.approve(approval_id, comment="go ahead after all"))
                    refused_before_effect(exc)
                    raise

        self.assertIn("was already refused before it began", answers[0]["message"])
        self.assertNotIn("may start", answers[0]["message"])
        self.assertEqual(await self.status(approval_id), "approved")

    async def test_a_withdraw_during_a_give_back_for_another_refusal_says_it_was_already_refused(self) -> None:
        decision, approval_id, go, task = await self.claimed_and_waiting()
        await self.manager.reject(approval_id, comment="wait")
        paused = self.pause_decision_until_the_give_back_starts("operator-approve")
        approve = asyncio.create_task(self.manager.approve(approval_id, comment="go ahead"), name="operator-approve")
        await asyncio.wait_for(paused.wait(), WAIT_SECONDS)

        self.page.url = OFF_LIST_URL  # refused before it begins, before the rejection could stop it
        go.set()
        with self.assertRaises(BrowserActionError):
            await asyncio.wait_for(task, WAIT_SECONDS)
        answer = await asyncio.wait_for(approve, WAIT_SECONDS)

        self.assertIn("was already refused before it began", answer["message"])
        self.assertEqual(await self.status(approval_id), "approved")

    async def test_a_withdraw_during_a_give_back_for_a_cancellation_is_not_told_the_action_may_start(self) -> None:
        # Cancelled while it waited (an agent job cancelled, the controller stopping), the action never began
        # and its approval goes back: the claim a withdraw then finds is no longer active, with no refusal on it.
        decision, approval_id, go, task = await self.claimed_and_waiting()
        await self.manager.reject(approval_id, comment="wait")
        paused = self.pause_decision_until_the_give_back_starts("operator-approve")
        approve = asyncio.create_task(self.manager.approve(approval_id, comment="go ahead"), name="operator-approve")
        await asyncio.wait_for(paused.wait(), WAIT_SECONDS)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT_SECONDS)
        answer = await asyncio.wait_for(approve, WAIT_SECONDS)

        self.assertIn("was already refused before it began", answer["message"])
        self.assertNotIn("may start", answer["message"])
        self.assertEqual((await self.status(approval_id), self.page.clicks), ("approved", 0))

    async def test_a_second_execution_while_the_claim_waits_is_told_to_wait_not_to_ask_again(self) -> None:
        decision, approval_id, go, task = await self.claimed_and_waiting()
        attempts = {
            "execute_approval": lambda: self.manager.execute_approval(approval_id),
            "execute_decision": lambda: asyncio.create_task(
                self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
            ),
            "claim": lambda: asyncio.create_task(self.manager.approvals.claim_execution(approval_id)),
        }

        for name, attempt in attempts.items():
            with self.subTest(path=name), self.assertRaises(PermissionError) as refused:
                await asyncio.wait_for(attempt(), WAIT_SECONDS)
            self.assertIn("is claimed by an action that has not begun", str(refused.exception))
            self.assertIn("do not ask for a new approval", str(refused.exception))
        go.set()
        await asyncio.wait_for(task, WAIT_SECONDS)
        with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
            await self.manager.execute_approval(approval_id)

    async def test_an_agents_approve_of_a_claimed_approval_is_refused_in_approve_terms(self) -> None:
        decision, approval_id, go, task = await self.claimed_and_waiting()

        with self.assertRaises(PermissionError) as approve:
            await self.manager.approvals.approve(approval_id, decided_via="agent")
        with self.assertRaises(PermissionError) as reject:
            await self.manager.approvals.reject(approval_id, decided_via="agent")
        go.set()
        await asyncio.wait_for(task, WAIT_SECONDS)

        self.assertIn("only an operator can approve it while the action has not begun", str(approve.exception))
        self.assertNotIn("reject", str(approve.exception))
        self.assertIn("only an operator can reject it before the action begins", str(reject.exception))


class CancelledWriteBackTests(ClaimWindowBase):
    """A cancellation while a refused action's approval is written back: the record and the audit agree.

    The write runs in a worker thread, which a cancellation does not stop. The
    give-back used to be cancelled around it: the record landed approved or
    rejected, but the hold ended as spent, which dropped a sensitive text and
    audited an operator's reject as not applied.
    """

    SIGN_IN = BrowserActionDecision(
        action="type",
        reason="sign in",
        selector="#password",
        text="hunter2",
        sensitive=True,
        risk_category="account_change",
    )

    def pause_the_write_back(self, approval_id: str) -> tuple[asyncio.Event, asyncio.Event, list[asyncio.Future]]:
        """Pause the give-back's write of the record once it is under way: (writing, release, the writes)."""
        store = self.manager.approvals
        writing, release = asyncio.Event(), asyncio.Event()
        writes: list[asyncio.Future] = []
        persist = store._persist

        async def paused_persist(approval: Any) -> None:
            if approval.id != approval_id or approval.status == "executed" or writing.is_set():
                return await persist(approval)
            # Its own task, as the thread is: the write lands whatever happens to its caller.
            write = asyncio.ensure_future(persist(approval))
            writes.append(write)
            writing.set()
            await release.wait()
            await write

        store._persist = paused_persist  # type: ignore[method-assign]
        return writing, release, writes

    async def cancel_while_it_is_written_back(
        self, task: asyncio.Task, writing: asyncio.Event, release: asyncio.Event, writes: list[asyncio.Future]
    ) -> None:
        await asyncio.wait_for(writing.wait(), WAIT_SECONDS)
        task.cancel()
        await asyncio.sleep(0)  # the cancellation reaches the task while the write is under way
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT_SECONDS)
        await asyncio.wait_for(asyncio.gather(*writes), WAIT_SECONDS)

    async def given_back(self, approval_id: str) -> list[tuple[str, Any]]:
        events = await self.manager.list_audit_events(limit=50, event_type="approval_given_back")
        return [
            (event["status"], event["details"].get("comment"))
            for event in events
            if event["approval_id"] == approval_id
        ]

    async def test_a_cancellation_while_it_is_written_back_approved_keeps_its_text_and_audit(self) -> None:
        approval_id = await self.approved(self.SIGN_IN)
        writing, release, writes = self.pause_the_write_back(approval_id)
        self.page.url = OFF_LIST_URL  # refused before the typing begins: the give-back starts
        task = asyncio.create_task(self.manager.execute_decision(SESSION, self.SIGN_IN, approval_id=approval_id))

        await self.cancel_while_it_is_written_back(task, writing, release, writes)

        self.assertEqual(await self.given_back(approval_id), [("approved", None)])
        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual(approval.status, "approved")
        self.assertEqual(self.manager.approvals.executable_action(approval).text, "hunter2")

    async def test_a_cancellation_while_the_give_back_is_audited_still_audits_it(self) -> None:
        approval_id = await self.approved(self.SIGN_IN)
        auditing, release = asyncio.Event(), asyncio.Event()
        append = self.manager.audit.append

        async def paused_append(**event: Any) -> Any:
            if event.get("event_type") == "approval_given_back":
                auditing.set()
                await release.wait()
            return await append(**event)

        self.manager.audit.append = paused_append  # type: ignore[method-assign]
        self.page.url = OFF_LIST_URL  # refused before the typing begins: the give-back starts
        task = asyncio.create_task(self.manager.execute_decision(SESSION, self.SIGN_IN, approval_id=approval_id))
        await asyncio.wait_for(auditing.wait(), WAIT_SECONDS)
        task.cancel()
        await asyncio.sleep(0)  # the cancellation reaches the task while the audit is under way
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT_SECONDS)

        self.assertEqual(await self.given_back(approval_id), [("approved", None)])
        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual(self.manager.approvals.executable_action(approval).text, "hunter2")

    async def test_a_cancellation_while_a_rejection_is_written_back_audits_it_as_applied(self) -> None:
        _, approval_id, go, task = await self.claimed_and_waiting()
        await self.manager.reject(approval_id, comment="stop")  # deferred on the claim
        writing, release, writes = self.pause_the_write_back(approval_id)
        go.set()  # the rejection refuses the click before it begins: the give-back starts

        await self.cancel_while_it_is_written_back(task, writing, release, writes)

        self.assertEqual(await self.given_back(approval_id), [("rejected", "stop")])
        decisions = await self.manager.list_audit_events(limit=50, event_type="approval_decision")
        self.assertNotIn("not_applied", [event["status"] for event in decisions])
        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual((approval.status, approval.decision_comment), ("rejected", "stop"))

    async def test_a_write_back_that_fails_while_its_caller_is_cancelled_ends_cancelled(self) -> None:
        # F5: the write's error replaced the cancellation, so a cancelled job ended with an OSError, and a
        # deadline around it surfaced as one. The approval stays spent either way.
        decision, approval_id = await self.approved_click()
        store = self.manager.approvals
        writing, release = asyncio.Event(), asyncio.Event()
        persist = store._persist

        async def failing_write_back(approval: Any) -> None:
            if approval.id != approval_id or approval.status == "executed":
                return await persist(approval)
            writing.set()
            await release.wait()
            raise OSError(28, "No space left on device")

        store._persist = failing_write_back  # type: ignore[method-assign]
        self.page.url = OFF_LIST_URL  # refused before the click begins: the give-back starts
        task = asyncio.create_task(self.manager.execute_decision(SESSION, decision, approval_id=approval_id))
        await asyncio.wait_for(writing.wait(), WAIT_SECONDS)
        with self.assertLogs("app.approval_execution", "WARNING") as logged:
            task.cancel()
            await asyncio.sleep(0)  # the cancellation reaches the task while the write is under way
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, WAIT_SECONDS)

        self.assertTrue(task.cancelled())
        self.assertIn("No space left on device", "\n".join(logged.output))
        self.assertEqual((await self.status(approval_id), self.page.clicks), ("executed", 0))

    async def test_a_rejection_whose_write_back_fails_stays_spent_and_is_audited_as_not_applied(self) -> None:
        _, approval_id, go, task = await self.claimed_and_waiting()
        await self.manager.reject(approval_id, comment="stop")
        store = self.manager.approvals
        persist = store._persist

        async def failing_persist(approval: Any) -> None:
            if approval.id == approval_id and approval.status == "rejected":
                raise OSError("disk full")
            await persist(approval)

        store._persist = failing_persist  # type: ignore[method-assign]
        go.set()
        with self.assertRaises(OSError):
            await asyncio.wait_for(task, WAIT_SECONDS)

        self.assertEqual(await self.status(approval_id), "executed")
        decisions = await self.manager.list_audit_events(limit=50, event_type="approval_decision")
        self.assertIn(
            ("not_applied", "stop"), [(event["status"], event["details"].get("comment")) for event in decisions]
        )


class CancelledClaimTests(ClaimWindowBase):
    """A cancellation while the claim itself is written: it burned the approval with nothing run.

    The claim awaited its persisted write before it made its hold, so a cancellation there left the
    record executed, with no hold to give it back and no audit entry; the operator's reject was told
    "has already been spent: its action started", and the agent to ask for a new approval. The write
    now lands whatever happens to its caller, and the hold is made with it.
    """

    def pause_the_claim(self, approval_id: str) -> tuple[asyncio.Event, asyncio.Event, list[asyncio.Future]]:
        """Pause the claim's write of the record (executed) once it is under way: (writing, release, the writes)."""
        store = self.manager.approvals
        writing, release = asyncio.Event(), asyncio.Event()
        writes: list[asyncio.Future] = []
        persist = store._persist

        async def paused_persist(approval: Any) -> None:
            if approval.id != approval_id or approval.status != "executed" or writing.is_set():
                return await persist(approval)
            # Its own task, as the thread is: the write lands whatever happens to its caller.
            write = asyncio.ensure_future(persist(approval))
            writes.append(write)
            writing.set()
            await release.wait()
            await write

        store._persist = paused_persist  # type: ignore[method-assign]
        return writing, release, writes

    async def cancel_while_it_is_claimed(self, approval_id: str, run: Any) -> None:
        """Start ``run()``, cancel it while the claim of ``approval_id`` is written, and see it end cancelled."""
        writing, release, writes = self.pause_the_claim(approval_id)
        task = asyncio.create_task(run())
        await asyncio.wait_for(writing.wait(), WAIT_SECONDS)
        task.cancel()
        await asyncio.sleep(0)  # the cancellation reaches the task while the write is under way
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT_SECONDS)
        await asyncio.wait_for(asyncio.gather(*writes), WAIT_SECONDS)

    async def audited(self, approval_id: str, event_type: str) -> list[str]:
        events = await self.manager.list_audit_events(limit=50, event_type=event_type)
        return [event["status"] for event in events if event["approval_id"] == approval_id]

    async def test_a_cancellation_while_the_claim_is_written_gives_the_approval_back(self) -> None:
        sign_in = CancelledWriteBackTests.SIGN_IN
        approval_id = await self.approved(sign_in)

        await self.cancel_while_it_is_claimed(
            approval_id, lambda: self.manager.execute_decision(SESSION, sign_in, approval_id=approval_id)
        )

        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual((approval.status, approval.executed_at), ("approved", None))
        self.assertEqual(await self.audited(approval_id, "approval_given_back"), ["approved"])
        self.assertEqual(self.manager.approvals.executable_action(approval).text, "hunter2")
        self.assertEqual((self.page.keyboard.typed, self.manager.approvals._claims), ([], {}))

    async def test_after_a_cancelled_claim_the_agents_retry_runs_once(self) -> None:
        decision, approval_id = await self.approved_click()

        await self.cancel_while_it_is_claimed(
            approval_id, lambda: self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        )

        self.assertEqual(self.page.clicks, 0)
        self.assertEqual(await self.retry(decision, approval_id), "ran")
        self.assertEqual((self.page.clicks, await self.status(approval_id)), (1, "executed"))

    async def test_after_a_cancelled_claim_an_operators_reject_applies(self) -> None:
        decision, approval_id = await self.approved_click()

        await self.cancel_while_it_is_claimed(
            approval_id, lambda: self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        )
        await self.manager.reject(approval_id, comment="not now")

        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual((approval.status, approval.decision_comment), ("rejected", "not now"))

    async def test_a_cancelled_claim_outside_a_give_back_block_stays_spent_and_is_audited(self) -> None:
        # As for a governed tool call: its block does not give back, so nothing says the approval was
        # spent unrun but this entry.
        _, approval_id = await self.approved_click()

        async def held() -> None:
            async with held_for_execution(self.manager.approvals, approval_id):
                raise AssertionError("the block ran")

        await self.cancel_while_it_is_claimed(approval_id, held)

        self.assertEqual(await self.status(approval_id), "executed")
        self.assertEqual(await self.audited(approval_id, "approval_claim_cancelled"), ["spent"])
        self.assertEqual(await self.audited(approval_id, "approval_given_back"), [])
        self.assertEqual(self.manager.approvals._claims, {})


class RunThroughShutdownTests(unittest.TestCase):
    """A shutdown that cancels a give-back's write as well as its caller: the caller ends, cancelled.

    _run_through shields the write, and waits for it through its caller's
    cancellations. Once the write itself is cancelled, as asyncio.run cancels
    every task at shutdown, it must stop waiting: awaiting a cancelled task
    raises at once, so without the check it spun the event loop, and the
    controller never exited (PDT-8).
    """

    def test_a_write_cancelled_with_its_caller_ends_the_caller_cancelled(self) -> None:
        ended: list[str] = []

        async def shutdown_during_a_write() -> None:
            writing = asyncio.Event()

            async def write() -> None:
                writing.set()
                await asyncio.sleep(WAIT_SECONDS)  # a give-back's write, under way

            caller = asyncio.ensure_future(_run_through(write()))
            await writing.wait()
            for task in asyncio.all_tasks() - {asyncio.current_task()}:  # the caller and its write, as at shutdown
                task.cancel()
            try:
                await caller
            except asyncio.CancelledError:
                ended.append("cancelled")

        # On a loop in a thread of its own, joined within a bound: no wait on a loop that spins ever times out.
        loop = threading.Thread(target=asyncio.run, args=(shutdown_during_a_write(),), daemon=True)
        loop.start()
        loop.join(WAIT_SECONDS)

        self.assertEqual((loop.is_alive(), ended), (False, ["cancelled"]))
