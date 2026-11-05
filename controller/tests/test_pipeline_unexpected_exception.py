"""An action that began and then ended in an unexpected exception or a cancellation still leaves a record.

Only the errors the pipeline knows (BrowserActionError, PlaywrightError, a policy block) were recorded. Anything
else, and a cancellation, propagated with no audit entry and no receipt, so the audit trail had no trace of an
action whose effect may have happened.
"""

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager, BrowserSession
from app.utils import UTC
from tests.test_browser_manager_actions import FakeContext, FakePage, _settings


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


if __name__ == "__main__":
    unittest.main()
