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

from app.action_errors import SESSION_CLOSED_GATE_LOST, SessionNotFoundError
from app.browser_manager import BrowserManager
from app.config import Settings
from app.models import McpToolCallRequest, SessionRecord
from app.tool_gateway import McpToolGateway

# A caller's id as long as a request line lets it be.
LONG_ID = "x" + "y" * 59_999


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


class _RealManagerTestCase(unittest.IsolatedAsyncioTestCase):
    """A real BrowserManager over temporary data roots, with no browser."""

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


class SessionLookupTests(_RealManagerTestCase):
    async def test_each_reason_gets_its_own_code_and_message(self) -> None:
        await self.manager.session_store.upsert(_record("sess-closed", "closed"))
        await self.manager.session_store.upsert(_record("sess-lost", "interrupted"))
        await self.manager.session_store.upsert(_record("sess-elsewhere", "active"))

        cases = {
            "sess-closed": ("session_closed", "is closed"),
            "sess-lost": ("session_interrupted", "cannot be resumed"),
            # e.g. owned by another controller sharing a Redis session store
            "sess-elsewhere": ("session_not_live", "not running on this controller"),
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

    def test_each_message_cuts_a_long_id_as_the_route_404s_cut_one(self) -> None:
        # Each message named the caller's id whole, and the REST 404 and an MCP tool's error are that message.
        for kwargs in (
            {},
            {"status": "closed"},
            {"status": "closed", "reason": SESSION_CLOSED_GATE_LOST},
            {"status": "interrupted"},
            {"status": "active"},
        ):
            with self.subTest(**kwargs):
                ordinary, long = SessionNotFoundError("sess-1", **kwargs), SessionNotFoundError(LONG_ID, **kwargs)

                self.assertEqual(long.message, ordinary.message.replace("sess-1", LONG_ID[:200] + "..."))
                self.assertEqual(str(long), long.message)
                self.assertEqual((long.code, long.reason, long.session_id), (ordinary.code, ordinary.reason, LONG_ID))

    async def test_get_record_still_returns_closed_records(self) -> None:
        await self.manager.session_store.upsert(_record("sess-closed", "closed"))

        record = await self.manager.get_session_record("sess-closed")

        self.assertEqual(record["status"], "closed")
        with self.assertRaises(SessionNotFoundError):
            await self.manager.get_session_record("sess-never")


class MissingFileMessageTests(_RealManagerTestCase):
    """Lookups that miss name what the caller asked for, not a path under a data root."""

    async def test_a_missing_auth_profile_is_named(self) -> None:
        with self.assertRaises(FileNotFoundError) as caught:
            self.manager.auth_profiles.resolve_state_path("shop", must_exist=True)

        self.assertIn("No saved auth profile 'shop'", str(caught.exception))
        self.assertIn("browser.list_auth_profiles", str(caught.exception))
        self.assertNotIn(self.tmp.name, str(caught.exception))

    async def test_a_missing_upload_is_named(self) -> None:
        with self.assertRaises(FileNotFoundError) as caught:
            await self.manager.uploads.safe_path("receipt.pdf")

        self.assertIn("'receipt.pdf'", str(caught.exception))
        self.assertIn("UPLOAD_ROOT", str(caught.exception))
        self.assertNotIn(self.tmp.name, str(caught.exception))
        self.assertIsNone(caught.exception.errno)


class GatewaySurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_error_carries_the_reason_and_code(self) -> None:
        manager = MagicMock()
        manager.observe = AsyncMock(side_effect=SessionNotFoundError("sess-closed", status="closed"))
        manager.ensure_session_accessible = AsyncMock()
        gateway = McpToolGateway(manager=manager, orchestrator=MagicMock(), job_queue=MagicMock())

        response = await gateway.call_tool(
            McpToolCallRequest(name="browser.observe", arguments={"session_id": "sess-closed"})
        )

        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["code"], "session_closed")
        self.assertEqual(response.structuredContent["session_id"], "sess-closed")
        self.assertIn("is closed", response.content[0].text)

    async def test_f9a_mcp_error_carries_the_close_reason(self) -> None:
        manager = MagicMock()
        manager.observe = AsyncMock(
            side_effect=SessionNotFoundError("sess-lost", status="closed", reason="request_gate_lost")
        )
        manager.ensure_session_accessible = AsyncMock()
        gateway = McpToolGateway(manager=manager, orchestrator=MagicMock(), job_queue=MagicMock())

        response = await gateway.call_tool(
            McpToolCallRequest(name="browser.observe", arguments={"session_id": "sess-lost"})
        )

        self.assertEqual(
            (response.structuredContent["code"], response.structuredContent["reason"]),
            ("session_closed", "request_gate_lost"),
        )

    async def test_f9a_rest_error_carries_the_close_reason(self) -> None:
        import json

        import app.main as main_module

        response = await main_module.handle_key_not_found(
            MagicMock(), SessionNotFoundError("sess-lost", status="closed", reason="request_gate_lost")
        )

        body = json.loads(response.body)
        self.assertEqual(
            (response.status_code, body["code"], body["reason"]), (404, "session_closed", "request_gate_lost")
        )


class RestSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        import app.main as main_module

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            patch.object(main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[]))
        )
        for service in (
            main_module.manager,
            main_module.job_queue,
            main_module.cron_service,
            main_module.maintenance,
        ):
            for method_name in ("startup", "shutdown"):
                stack.enter_context(patch.object(service, method_name, new=AsyncMock()))
        self.client = stack.enter_context(TestClient(main_module.app))

    def test_rest_404_carries_the_reason_and_code(self) -> None:
        response = self.client.get("/sessions/sess-never-existed")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "unknown_session")
        self.assertIn("No session with id sess-never-existed", response.json()["detail"])

    def test_a_long_id_is_cut_in_each_404_that_names_it(self) -> None:
        # The sessions router, a route behind it and the app-wide session check (require_session_access, which
        # refuses an id with no record under named credentials) answered a 60,000-character id in a detail of 60,079.
        detail = (
            f"No session with id {LONG_ID[:200] + '...'}. List sessions to find a live one, or create a new session."
        )
        with patch("app.browser.services.sessions.has_named_credentials", return_value=True):
            checked = self.client.get("/approvals", params={"session_id": LONG_ID})
        for name, response in (
            ("router", self.client.get(f"/sessions/{LONG_ID}")),
            ("observe", self.client.get(f"/sessions/{LONG_ID}/observe")),
            ("app-wide check", checked),
        ):
            with self.subTest(name):
                self.assertEqual(
                    (response.status_code, response.json()), (404, {"detail": detail, "code": "unknown_session"})
                )
                self.assertLess(len(response.content), 1024)


if __name__ == "__main__":
    unittest.main()
