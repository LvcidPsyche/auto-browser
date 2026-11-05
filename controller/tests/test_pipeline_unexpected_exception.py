"""An action that began and then ended in an unexpected exception or a cancellation still leaves a record.

Only the errors the pipeline knows (BrowserActionError, PlaywrightError, a policy block) were recorded. Anything
else, and a cancellation, propagated with no audit entry and no receipt, so the audit trail had no trace of an
action whose effect may have happened. Round 4: that held only while the operation ran; one that ended while the
page was read after the action, while a known failure was handled, or between the record's own writes still left
no record, or part of one.
"""

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from playwright.async_api import Error as PlaywrightError

from app.action_errors import BrowserActionError
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


if __name__ == "__main__":
    unittest.main()
