"""An action that began and then ended in an unexpected exception or a cancellation still leaves a record.

Only the errors the pipeline knows (BrowserActionError, PlaywrightError, a policy block) were recorded. Anything
else, and a cancellation, propagated with no audit entry and no receipt, so the audit trail had no trace of an
action whose effect may have happened. Round 4: that held only while the operation ran; one that ended while the
page was read after the action, while a known failure was handled, or between the record's own writes still left
no record, or part of one.
"""

import asyncio
import errno
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from playwright.async_api import Error as PlaywrightError

from app.action_errors import BrowserActionError
from app.action_records import drain_unfinished_records, write_record
from app.approvals import held_for_execution
from app.browser_manager import BrowserManager, BrowserSession
from app.models import BrowserActionDecision
from app.utils import UTC
from tests.test_browser_manager_actions import FakeContext, FakePage, _settings

# Long enough for a loaded machine; what the tests wait on is an event, not a duration.
WAIT_SECONDS = 30


class PipelineUnexpectedExceptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        settings = _settings(root)
        settings.witness_enabled = True
        self.manager = BrowserManager(settings)
        await self.manager.witness.startup()
        self.manager.audit.append = AsyncMock()
        self.manager._append_jsonl = AsyncMock()
        self.manager._persist_session = AsyncMock()
        page = FakePage()
        dirs = {name: root / name / "s1" for name in ("artifacts", "auth", "uploads")}
        for folder in dirs.values():
            folder.mkdir(parents=True)
        self.session = BrowserSession(
            id="s1",
            name="fake",
            created_at=datetime.now(UTC),
            context=FakeContext(page),
            page=page,
            artifact_dir=dirs["artifacts"],
            auth_dir=dirs["auth"],
            upload_dir=dirs["uploads"],
            takeover_url="http://x",
            trace_path=dirs["artifacts"] / "trace.zip",
        )
        self.manager.sessions["s1"] = self.session
        self.manager._light_snapshot = AsyncMock(return_value={"url": "https://example.com", "title": "t"})
        self.manager._observation_payload = AsyncMock(return_value={"url": "https://example.com", "title": "t"})
        self.manager._settle = AsyncMock()
        self.manager._ensure_witness_remote_ready = AsyncMock()
        self.manager._check_bot_challenge = AsyncMock(return_value=None)

    async def asyncTearDown(self) -> None:
        # A cancelled action's record finishes behind it: its witness write must not outlive the temp dir.
        await drain_unfinished_records()
        self.tmp.cleanup()

    async def _receipts_and_audit_statuses(self):
        receipts = await self.manager.witness.list("s1", limit=10)
        audited = [call.kwargs["status"] for call in self.manager.audit.append.await_args_list]
        return receipts, audited

    async def test_an_unexpected_exception_after_the_change_began_is_recorded_and_re_raised(self) -> None:
        async def operation():
            raise RuntimeError("unexpected bug with a secret detail")

        with self.assertRaises(RuntimeError):
            await self.manager._run_action(self.session, "click", {"selector": "#b"}, operation)

        receipts, audited = await self._receipts_and_audit_statuses()
        self.assertEqual([receipt.status for receipt in receipts], ["error"])
        self.assertEqual(audited, ["error"])
        self.assertIn("outcome unknown", str(receipts[0].metadata["error"]))
        self.assertNotIn("secret detail", str(receipts[0].model_dump()))

    async def test_a_cancellation_after_the_change_began_is_recorded_and_re_raised(self) -> None:
        started = asyncio.Event()

        async def operation():
            started.set()
            await asyncio.sleep(30)

        task = asyncio.create_task(self.manager._run_action(self.session, "click", {"selector": "#b"}, operation))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        receipts, audited = await self._receipts_and_audit_statuses()
        self.assertEqual([receipt.status for receipt in receipts], ["error"])
        self.assertEqual(audited, ["error"])
        self.assertIn("cancelled", str(receipts[0].metadata["error"]))

    async def test_r5c_13_an_action_that_ran_returns_its_result_when_its_session_record_cannot_be_saved(self) -> None:
        # The action ran and was recorded, then the save of the session's record failed (a full disk), and that error
        # reached the caller, which read a failed action: an agent that retried acted twice.
        del self.manager._persist_session  # the real one, over a store that cannot write
        self.manager.session_store.upsert = AsyncMock(side_effect=OSError(errno.ENOSPC, "No space left on device"))
        ran: list[int] = []

        async def operation():
            ran.append(1)

        with self.assertLogs("app.actions.pipeline", level="WARNING") as logs:
            result = await self.manager._run_action(self.session, "click", {"selector": "#b"}, operation)

        self.assertEqual(ran, [1])
        self.assertEqual(result["action"], "click")
        self.manager.session_store.upsert.assert_awaited_once()
        receipts, audited = await self._receipts_and_audit_statuses()
        self.assertEqual(([receipt.status for receipt in receipts], audited), (["ok"], ["ok"]))
        self.assertTrue(any("s1" in line and "No space left on device" in line for line in logs.output), logs.output)

    async def test_a_failing_record_does_not_replace_the_original_exception(self) -> None:
        self.manager.audit.append = AsyncMock(side_effect=OSError("disk full"))

        async def operation():
            raise RuntimeError("unexpected bug")

        with self.assertRaises(RuntimeError):
            await self.manager._run_action(self.session, "click", {"selector": "#b"}, operation)

    async def test_a_cancellation_while_the_page_is_read_after_the_action_says_the_action_ran(self) -> None:
        reading = asyncio.Event()

        async def observe(*_: Any, **__: Any) -> dict[str, Any]:
            reading.set()
            await asyncio.sleep(WAIT_SECONDS)
            return {"url": "https://example.com"}

        self.manager._observation_payload = AsyncMock(side_effect=observe)

        async def operation():
            return None

        task = asyncio.create_task(self.manager._run_action(self.session, "click", {"selector": "#b"}, operation))
        await asyncio.wait_for(reading.wait(), WAIT_SECONDS)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT_SECONDS)

        receipts, audited = await self._receipts_and_audit_statuses()
        self.assertEqual([receipt.status for receipt in receipts], ["error"])
        self.assertEqual(audited, ["error"])
        self.assertEqual(self.manager._append_jsonl.await_count, 1)
        self.assertEqual(
            receipts[0].metadata["error"],
            "the action ran; reading the page after it was cancelled. Observe the page before acting again.",
        )
        self.assertEqual(self.manager._observation_payload.await_count, 1)  # not read again to record it

    async def test_an_unexpected_error_while_a_known_failure_is_handled_is_recorded_once(self) -> None:
        before = {"url": "https://example.com", "title": "t"}
        self.manager._light_snapshot = AsyncMock(side_effect=[before, RuntimeError("snapshot bug")])

        async def operation():
            raise PlaywrightError("Timeout 5000ms exceeded.")

        with self.assertRaises(RuntimeError):
            await self.manager._run_action(self.session, "click", {"selector": "#b"}, operation)

        receipts, audited = await self._receipts_and_audit_statuses()
        self.assertEqual([receipt.status for receipt in receipts], ["error"])
        self.assertEqual(audited, ["error"])
        self.assertIn("outcome unknown: the action began and ended in RuntimeError", receipts[0].metadata["error"])
        self.assertNotIn("snapshot bug", str(receipts[0].model_dump()))

    async def test_a_known_failure_is_recorded_once(self) -> None:
        async def operation():
            raise BrowserActionError("element not found", action="click", code="element_not_found")

        with self.assertRaises(BrowserActionError):
            await self.manager._run_action(self.session, "click", {"selector": "#b"}, operation)

        receipts, audited = await self._receipts_and_audit_statuses()
        self.assertEqual([receipt.status for receipt in receipts], ["failed"])
        self.assertEqual(audited, ["failed"])
        self.assertEqual(self.manager._append_jsonl.await_count, 1)

    async def test_a_cancellation_between_the_records_writes_still_leaves_all_three(self) -> None:
        writing, finish, receipted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        audited: list[str] = []
        receipt = self.manager._record_witness_receipt

        async def slow_audit(**event: Any) -> None:
            writing.set()
            await finish.wait()
            audited.append(event["status"])

        async def marked_receipt(session: Any, **event: Any) -> None:
            await receipt(session, **event)
            receipted.set()

        self.manager.audit.append = AsyncMock(side_effect=slow_audit)
        self.manager._record_witness_receipt = marked_receipt  # type: ignore[method-assign]

        async def operation():
            return None

        task = asyncio.create_task(self.manager._run_action(self.session, "click", {"selector": "#b"}, operation))
        await asyncio.wait_for(writing.wait(), WAIT_SECONDS)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT_SECONDS)
        finish.set()
        await asyncio.wait_for(receipted.wait(), WAIT_SECONDS)  # the writes finish behind the cancellation

        receipts = await self.manager.witness.list("s1", limit=10)
        self.assertEqual([receipt.status for receipt in receipts], ["ok"])
        self.assertEqual(audited, ["ok"])
        self.assertEqual(self.manager._append_jsonl.await_count, 1)

    async def test_a_failure_after_the_page_was_read_says_recording_it_ended(self) -> None:
        self.manager._action_verification = lambda *_: (_ for _ in ()).throw(RuntimeError("verification bug"))

        async def operation():
            return None

        with self.assertRaises(RuntimeError):
            await self.manager._run_action(self.session, "click", {"selector": "#b"}, operation)

        receipts, _ = await self._receipts_and_audit_statuses()
        self.assertEqual(
            receipts[0].metadata["error"],
            "the action ran; recording it ended in RuntimeError. Observe the page before acting again.",
        )
        self.assertNotIn("verification bug", str(receipts[0].model_dump()))

    async def test_the_next_action_records_after_a_cancelled_actions_slow_writes(self) -> None:
        writing, finish = asyncio.Event(), asyncio.Event()
        order: list[str] = []

        async def audit(**event: Any) -> None:
            if event["action"] == "click":
                writing.set()
                await finish.wait()  # the cancelled action's audit write, still going
            order.append(event["action"])

        self.manager.audit.append = AsyncMock(side_effect=audit)

        async def operation():
            return None

        first = asyncio.create_task(self.manager._run_action(self.session, "click", {"selector": "#a"}, operation))
        await asyncio.wait_for(writing.wait(), WAIT_SECONDS)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(first, WAIT_SECONDS)  # the lock is free; its writes are not done

        second = asyncio.create_task(self.manager._run_action(self.session, "hover", {"selector": "#b"}, operation))
        await asyncio.sleep(0.2)
        self.assertEqual(order, [], "the next action recorded before the cancelled one's writes finished")
        finish.set()
        await asyncio.wait_for(second, WAIT_SECONDS)

        self.assertEqual(order, ["click", "hover"])

    async def test_a_cancellation_while_an_unexpected_error_is_recorded_wins_over_the_error(self) -> None:
        # The first cancellation of the action: an operator's cancel must end the job cancelled, not as an error.
        writing, finish, audited = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def audit(**event: Any) -> None:
            writing.set()
            await finish.wait()
            audited.set()

        self.manager.audit.append = AsyncMock(side_effect=audit)

        async def operation():
            raise RuntimeError("unexpected bug")

        task = asyncio.create_task(self.manager._run_action(self.session, "click", {"selector": "#b"}, operation))
        await asyncio.wait_for(writing.wait(), WAIT_SECONDS)
        task.cancel()  # while the interrupted action's record is being written
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT_SECONDS)
        finish.set()
        await asyncio.wait_for(audited.wait(), WAIT_SECONDS)  # the record finishes behind it

        await drain_unfinished_records()
        receipts, _ = await self._receipts_and_audit_statuses()
        self.assertIn("ended in RuntimeError", receipts[0].metadata["error"])

    async def test_a_record_that_ended_cancelled_does_not_cancel_the_next_action(self) -> None:
        pending = asyncio.create_task(asyncio.sleep(WAIT_SECONDS))
        self.session.pending_record = pending
        ran: list[int] = []

        async def operation():
            ran.append(1)

        action = asyncio.create_task(self.manager._run_action(self.session, "click", {"selector": "#b"}, operation))
        await asyncio.sleep(0.2)
        self.assertEqual(ran, [], "the action started before the pending record finished")
        pending.cancel()  # the record itself ends cancelled (at shutdown, say)

        result = await asyncio.wait_for(action, WAIT_SECONDS)

        self.assertEqual((result["action"], ran), ("click", [1]))

    async def _cancel_an_action_mid_record(self, order: list[str], finish: asyncio.Event) -> None:
        """Cancel a click while its audit write waits on ``finish``; every audited action name goes to ``order``."""
        writing = asyncio.Event()

        async def audit(**event: Any) -> None:
            if event["action"] == "click":
                writing.set()
                await finish.wait()
            order.append(event["action"])

        self.manager.audit.append = AsyncMock(side_effect=audit)

        async def operation():
            return None

        action = asyncio.create_task(self.manager._run_action(self.session, "click", {"selector": "#a"}, operation))
        await asyncio.wait_for(writing.wait(), WAIT_SECONDS)
        action.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(action, WAIT_SECONDS)

    async def test_close_waits_for_a_cancelled_actions_record_before_its_own(self) -> None:
        order: list[str] = []
        finish = asyncio.Event()
        await self._cancel_an_action_mid_record(order, finish)
        self.session.context.close = AsyncMock()  # type: ignore[attr-defined]

        close = asyncio.create_task(self.manager.close_session("s1"))
        await asyncio.sleep(0.2)
        self.assertEqual(order, [], "the close was recorded before the cancelled action's record")
        finish.set()
        await asyncio.wait_for(close, WAIT_SECONDS)

        self.assertEqual(order, ["click", "close_session"])

    async def test_a_takeover_waits_for_a_cancelled_actions_record_before_its_own(self) -> None:
        order: list[str] = []
        finish = asyncio.Event()
        await self._cancel_an_action_mid_record(order, finish)

        takeover = asyncio.create_task(self.manager.request_human_takeover("s1", "check"))
        await asyncio.sleep(0.2)
        self.assertEqual(order, [], "the takeover was recorded before the cancelled action's record")
        finish.set()
        await asyncio.wait_for(takeover, WAIT_SECONDS)

        self.assertEqual(order, ["click", "request_human_takeover"])

    async def test_a_cancelled_takeover_request_still_writes_its_whole_record(self) -> None:
        writing, finish, receipted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        receipt = self.manager._record_witness_receipt

        async def slow_audit(**event: Any) -> None:
            writing.set()
            await finish.wait()

        async def marked_receipt(session: Any, **event: Any) -> None:
            await receipt(session, **event)
            receipted.set()

        self.manager.audit.append = AsyncMock(side_effect=slow_audit)
        self.manager._record_witness_receipt = marked_receipt  # type: ignore[method-assign]

        request = asyncio.create_task(self.manager.request_human_takeover("s1", "check"))
        await asyncio.wait_for(writing.wait(), WAIT_SECONDS)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(request, WAIT_SECONDS)
        finish.set()
        await asyncio.wait_for(receipted.wait(), WAIT_SECONDS)  # the receipt is written behind the cancellation

        receipts = await self.manager.witness.list("s1", limit=10)
        self.assertEqual([item.action for item in receipts], ["request_human_takeover"])
        self.assertEqual(self.manager._append_jsonl.await_count, 1)

    async def test_a_policy_block_whose_handling_fails_still_records_the_refusal(self) -> None:
        before = {"url": "https://example.com", "title": "t"}
        self.manager._ensure_witness_remote_ready = AsyncMock(side_effect=PermissionError("not ready"))
        self.manager._light_snapshot = AsyncMock(side_effect=[before, RuntimeError("snapshot bug")])
        ran: list[int] = []

        async def operation():
            ran.append(1)

        with self.assertRaises(RuntimeError):
            await self.manager._run_action(self.session, "click", {"selector": "#b"}, operation)

        receipts, audited = await self._receipts_and_audit_statuses()
        self.assertEqual([receipt.status for receipt in receipts], ["blocked"])
        self.assertEqual(audited, ["blocked"])
        self.assertEqual((self.manager._append_jsonl.await_count, ran), (1, []))
        self.assertEqual(self.manager._light_snapshot.await_count, 2)  # the record reads no page
        self.assertNotIn("snapshot bug", str(receipts[0].model_dump()))

    async def test_a_policy_block_whose_handling_is_cancelled_still_records_the_refusal(self) -> None:
        before = {"url": "https://example.com", "title": "t"}
        reading = asyncio.Event()

        async def snapshot(*_: Any, **__: Any) -> dict[str, Any]:
            if self.manager._light_snapshot.await_count == 1:
                return before
            reading.set()
            await asyncio.sleep(WAIT_SECONDS)
            return before

        self.manager._ensure_witness_remote_ready = AsyncMock(side_effect=PermissionError("not ready"))
        self.manager._light_snapshot = AsyncMock(side_effect=snapshot)

        async def operation():
            return None

        task = asyncio.create_task(self.manager._run_action(self.session, "click", {"selector": "#b"}, operation))
        await asyncio.wait_for(reading.wait(), WAIT_SECONDS)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT_SECONDS)
        await drain_unfinished_records()

        receipts, audited = await self._receipts_and_audit_statuses()
        self.assertEqual([receipt.status for receipt in receipts], ["blocked"])
        self.assertEqual(audited, ["blocked"])

    async def test_shutdown_waits_for_a_record_still_being_written(self) -> None:
        finish = asyncio.Event()
        wrote: list[int] = []

        async def writes() -> None:
            await finish.wait()
            wrote.append(1)

        caller = asyncio.create_task(write_record(SimpleNamespace(pending_record=None), writes))
        await asyncio.sleep(0)
        caller.cancel()  # the action that wrote it was cancelled; its writes go on
        with self.assertRaises(asyncio.CancelledError):
            await caller
        asyncio.get_running_loop().call_later(0.1, finish.set)

        await self.manager.shutdown()

        self.assertEqual(wrote, [1])

    async def test_an_unexpected_error_before_the_action_began_records_nothing_and_gives_its_approval_back(
        self,
    ) -> None:
        approvals = self.manager.approvals
        await approvals.startup()
        action = BrowserActionDecision(action="click", reason="pay", selector="#b", risk_category="payment")
        approval = await approvals.create_or_reuse_pending(session_id="s1", kind="payment", reason="r", action=action)
        await approvals.approve(approval.id)
        self.manager._ensure_witness_remote_ready = AsyncMock(side_effect=RuntimeError("remote check bug"))
        ran: list[int] = []

        async def operation():
            ran.append(1)

        with self.assertRaises(RuntimeError):
            async with held_for_execution(approvals, approval.id, may_give_back=True):
                await self.manager._run_action(self.session, "click", {"selector": "#b"}, operation)

        receipts = await self.manager.witness.list("s1", limit=10)
        audited = self.manager.audit.append.await_args_list
        self.assertEqual([receipt for receipt in receipts if receipt.event_type == "browser_action"], [])
        self.assertEqual([call for call in audited if call.kwargs["event_type"] == "browser_action"], [])
        self.assertEqual((self.manager._append_jsonl.await_count, ran), (0, []))
        self.assertEqual((await approvals.get(approval.id)).status, "approved")


class DrainUnfinishedRecordsTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_drain_gives_up_after_its_bound_and_says_what_was_left(self) -> None:
        never = asyncio.Event()

        async def writes() -> None:
            await never.wait()

        caller = asyncio.create_task(write_record(SimpleNamespace(pending_record=None), writes))
        await asyncio.sleep(0)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller

        with self.assertLogs("app.action_records", "WARNING") as logged:
            await asyncio.wait_for(drain_unfinished_records(timeout=0.1), WAIT_SECONDS)

        self.assertIn("1 action record", "\n".join(logged.output))
        never.set()
        await drain_unfinished_records()


if __name__ == "__main__":
    unittest.main()
