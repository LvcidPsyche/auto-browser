"""An unusable session id must say why, not just echo itself.

Lookups raised ``KeyError(session_id)``, which the MCP gateway surfaced as
``{"error": "ec7fba8336ac"}`` — an error whose entire text was the id. The
replacement stays a KeyError (every ``except KeyError`` keeps working) but
carries a reason and a machine-readable code.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.action_errors import SessionNotFoundError
from app.browser_manager import BrowserManager
from app.config import Settings
from app.models import McpToolCallRequest, SessionRecord
from app.tool_gateway import McpToolGateway


def _record(session_id: str, status: str) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        name=session_id,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        status=status,
        live=False,
        current_url="https://example.com",
        title="t",
        artifact_dir="/tmp",
        takeover_url="http://127.0.0.1:6080",
        remote_access={},
    )


class SessionLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.manager = BrowserManager(
            Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(root / "artifacts"),
                UPLOAD_ROOT=str(root / "uploads"),
                AUTH_ROOT=str(root / "auth"),
                APPROVAL_ROOT=str(root / "approvals"),
                AUDIT_ROOT=str(root / "audit"),
                SESSION_STORE_ROOT=str(root / "sessions"),
            )
        )
        await self.manager.session_store.startup()

    async def test_each_reason_gets_its_own_code_and_message(self) -> None:
        await self.manager.session_store.upsert(_record("sess-closed", "closed"))
        await self.manager.session_store.upsert(_record("sess-lost", "interrupted"))

        cases = {
            "sess-closed": ("session_closed", "is closed"),
            "sess-lost": ("session_interrupted", "cannot be resumed"),
            "sess-never": ("unknown_session", "No session with id sess-never"),
        }
        for session_id, (code, phrase) in cases.items():
            with self.subTest(session_id=session_id):
                with self.assertRaises(SessionNotFoundError) as caught:
                    await self.manager.get_session(session_id)
                self.assertEqual(caught.exception.code, code)
                self.assertIn(phrase, str(caught.exception))
                self.assertNotEqual(str(caught.exception), session_id)
                # Still a KeyError, so existing 404 handling is unchanged.
                self.assertIsInstance(caught.exception, KeyError)

    async def test_get_record_still_returns_closed_records(self) -> None:
        await self.manager.session_store.upsert(_record("sess-closed", "closed"))

        record = await self.manager.get_session_record("sess-closed")

        self.assertEqual(record["status"], "closed")
        with self.assertRaises(SessionNotFoundError):
            await self.manager.get_session_record("sess-never")


class GatewaySurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_error_carries_the_reason_and_code(self) -> None:
        manager = MagicMock()
        manager.observe = AsyncMock(side_effect=SessionNotFoundError("sess-closed", status="closed"))
        gateway = McpToolGateway(manager=manager, orchestrator=MagicMock(), job_queue=MagicMock())

        response = await gateway.call_tool(
            McpToolCallRequest(name="browser.observe", arguments={"session_id": "sess-closed"})
        )

        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["code"], "session_closed")
        self.assertEqual(response.structuredContent["session_id"], "sess-closed")
        self.assertIn("is closed", response.content[0].text)


class RestSurfaceTests(unittest.TestCase):
    def test_rest_404_carries_the_reason_and_code(self) -> None:
        import app.main as main_module

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[])
                )
            )
            for service in (
                main_module.manager,
                main_module.job_queue,
                main_module.cron_service,
                main_module.maintenance,
            ):
                for method_name in ("startup", "shutdown"):
                    stack.enter_context(patch.object(service, method_name, new=AsyncMock()))
            client = stack.enter_context(TestClient(main_module.app))

            response = client.get("/sessions/sess-never-existed")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "unknown_session")
        self.assertIn("No session with id sess-never-existed", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
