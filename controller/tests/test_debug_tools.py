from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.utils import UTC


class FakeTracing:
    def __init__(self) -> None:
        self.stop_calls: list[str | None] = []

    async def stop(self, path: str | None = None) -> None:
        self.stop_calls.append(path)
        if path:
            Path(path).write_text("trace", encoding="utf-8")


class FakeContext:
    def __init__(self) -> None:
        self.tracing = FakeTracing()

    async def close(self) -> None:
        return None


class FakePage:
    def __init__(self, url: str = "https://example.com") -> None:
        self.url = url

    async def title(self) -> str:
        return "Example Domain"


class DebugToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.settings = Settings(_env_file=None)
        self.settings.artifact_root = str(root / "artifacts")
        self.settings.upload_root = str(root / "uploads")
        self.settings.auth_root = str(root / "auth")
        self.settings.approval_root = str(root / "approvals")
        self.settings.session_store_root = str(root / "sessions")
        self.settings.audit_root = str(root / "audit")
        self.settings.enable_tracing = True
        self.manager = BrowserManager(self.settings)
        await self.manager.session_store.startup()

        artifact_dir = Path(self.settings.artifact_root) / "session-debug"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.session = BrowserSession(
            id="session-debug",
            name="session-debug",
            created_at=datetime.now(UTC),
            context=FakeContext(),  # type: ignore[arg-type]
            page=FakePage(),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(self.settings.auth_root) / "session-debug",
            upload_dir=Path(self.settings.upload_root) / "session-debug",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
            trace_recording=True,
        )
        self.session.auth_dir.mkdir(parents=True, exist_ok=True)
        self.session.upload_dir.mkdir(parents=True, exist_ok=True)
        self.session.console_messages.extend(
            [
                {"type": "info", "text": "first"},
                {"type": "error", "text": "second"},
                {"type": "warning", "text": "third"},
            ]
        )
        self.session.page_errors.extend(["first error", "second error", "third error"])
        self.session.request_failures.extend(
            [
                {"url": "https://example.com/a", "failure": "net::ERR_ABORTED"},
                {"url": "https://example.com/b", "failure": "net::ERR_FAILED"},
                {"url": "https://example.com/c", "failure": "net::ERR_TIMED_OUT"},
            ]
        )
        self.manager.sessions[self.session.id] = self.session

    async def asyncTearDown(self) -> None:
        await self.manager.session_store.shutdown()
        self.tempdir.cleanup()

    async def test_debug_tail_helpers_return_bounded_items(self) -> None:
        console_payload = await self.manager.get_console_messages("session-debug", limit=2)
        page_error_payload = await self.manager.get_page_errors("session-debug", limit=2)
        request_failure_payload = await self.manager.get_request_failures("session-debug", limit=2)

        self.assertEqual([item["text"] for item in console_payload["items"]], ["second", "third"])
        self.assertEqual(page_error_payload["items"], ["second error", "third error"])
        self.assertEqual(
            [item["url"] for item in request_failure_payload["items"]],
            ["https://example.com/b", "https://example.com/c"],
        )

    async def test_stop_trace_finalizes_trace_once(self) -> None:
        payload = await self.manager.stop_trace("session-debug")

        self.assertFalse(payload["trace_recording"])
        self.assertTrue(payload["trace_exists"])
        self.assertEqual(payload["trace_url"], "/artifacts/session-debug/trace.zip")
        self.assertEqual(self.session.context.tracing.stop_calls, [str(self.session.trace_path)])  # type: ignore[attr-defined]

        second_payload = await self.manager.stop_trace("session-debug")
        self.assertFalse(second_payload["trace_recording"])
        self.assertEqual(self.session.context.tracing.stop_calls, [str(self.session.trace_path)])  # type: ignore[attr-defined]
