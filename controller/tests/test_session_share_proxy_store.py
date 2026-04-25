from __future__ import annotations

import atexit
import base64
import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="auto-browser-share-http-"))
atexit.register(lambda: shutil.rmtree(_TEST_ROOT, ignore_errors=True))
for env_name, relative_path in {
    "ARTIFACT_ROOT": "artifacts",
    "UPLOAD_ROOT": "uploads",
    "AUTH_ROOT": "auth",
    "APPROVAL_ROOT": "approvals",
    "AUDIT_ROOT": "audit",
    "SESSION_STORE_ROOT": "sessions",
    "JOB_STORE_ROOT": "jobs",
    "MCP_SESSION_STORE_PATH": "mcp/sessions.json",
    "CRON_STORE_PATH": "crons/crons.json",
    "REMOTE_ACCESS_INFO_PATH": "tunnels/reverse-ssh.json",
}.items():
    os.environ.setdefault(env_name, str(_TEST_ROOT / relative_path))

import app.main as main_module
from app.proxy_personas import ProxyPersonaStore
from app.session_share import SCOPE_OBSERVE, SessionShareManager


class SessionShareManagerTests(unittest.TestCase):
    def test_round_trip_token(self) -> None:
        manager = SessionShareManager(secret="secret123", ttl_minutes=10)

        token = manager.create_token("session-1", ttl_seconds=90)
        payload = manager.validate_token(token["token"])

        self.assertEqual(payload["session_id"], "session-1")
        self.assertEqual(payload["scope"], SCOPE_OBSERVE)
        self.assertGreaterEqual(token["expires_in_seconds"], 89)

    def test_rejects_non_positive_constructor_ttl(self) -> None:
        with self.assertRaisesRegex(ValueError, "ttl_minutes must be positive"):
            SessionShareManager(secret="secret123", ttl_minutes=0)

    def test_rejects_invalid_token_payload_types(self) -> None:
        manager = SessionShareManager(secret="secret123", ttl_minutes=10)
        payload = {"session_id": "session-1", "exp": "not-an-int", "scope": SCOPE_OBSERVE}
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"{payload_b64}.{manager._sign(payload_b64)}"

        info = manager.token_info(token)

        self.assertFalse(info["valid"])
        self.assertIn("token exp must be an integer", info["error"])

    def test_rejects_unsupported_scope(self) -> None:
        manager = SessionShareManager(secret="secret123", ttl_minutes=10)

        with self.assertRaisesRegex(ValueError, "unsupported share scope"):
            manager.create_token("session-1", scope="write")

    def test_rejects_non_positive_custom_ttl(self) -> None:
        manager = SessionShareManager(secret="secret123", ttl_minutes=10)

        with self.assertRaisesRegex(ValueError, "ttl_seconds must be positive"):
            manager.create_token("session-1", ttl_seconds=0)


class ProxyPersonaStoreTests(unittest.TestCase):
    def test_list_personas_sorts_and_skips_invalid_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "proxy-personas.json"
            path.write_text(
                json.dumps(
                    {
                        "z-west": {"server": "http://z.example.com:8080", "description": "Z"},
                        "broken-entry": "not-an-object",
                        "missing-server": {"username": "alice"},
                        "a-east": {"server": " http://a.example.com:8080 ", "password": "secret"},
                    }
                ),
                encoding="utf-8",
            )
            store = ProxyPersonaStore(path)

            personas = store.list_personas()

            self.assertEqual([item["name"] for item in personas], ["a-east", "z-west"])
            self.assertEqual(personas[0]["server"], "http://a.example.com:8080")
            self.assertTrue(personas[0]["has_password"])

    def test_set_persona_normalizes_values_and_delete_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "proxy-personas.json"
            store = ProxyPersonaStore(path)

            summary = store.set_persona(
                " us-east ",
                server=" http://proxy.example.com:8080 ",
                username=" alice ",
                password="",
                description=" primary route ",
            )
            resolved = store.resolve_proxy("us-east")
            deleted = store.delete_persona("us-east")

            self.assertEqual(summary["name"], "us-east")
            self.assertEqual(summary["server"], "http://proxy.example.com:8080")
            self.assertEqual(summary["username"], "alice")
            self.assertFalse(summary["has_password"])
            self.assertEqual(summary["description"], "primary route")
            self.assertEqual(resolved, {"server": "http://proxy.example.com:8080", "username": "alice"})
            self.assertTrue(deleted)
            self.assertFalse(store.delete_persona("us-east"))


class ShareHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[]))
        )
        for service, method_name in (
            (main_module.manager, "startup"),
            (main_module.manager, "shutdown"),
            (main_module.job_queue, "startup"),
            (main_module.job_queue, "shutdown"),
            (main_module.cron_service, "startup"),
            (main_module.cron_service, "shutdown"),
            (main_module.maintenance, "startup"),
            (main_module.maintenance, "shutdown"),
        ):
            self.stack.enter_context(patch.object(service, method_name, new=AsyncMock()))
        self.client = self.stack.enter_context(TestClient(main_module.app))

    def tearDown(self) -> None:
        self.stack.close()

    def test_share_session_uses_default_ttl_without_body(self) -> None:
        get_session = AsyncMock(return_value=SimpleNamespace(id="session-1"))
        create_token = Mock(return_value={"token": "tok", "session_id": "session-1"})

        with patch.object(main_module.manager, "get_session", get_session), patch.object(
            main_module.share_manager, "create_token", create_token
        ):
            response = self.client.post("/sessions/session-1/share")

        self.assertEqual(response.status_code, 200)
        create_token.assert_called_once_with("session-1", ttl_seconds=3600)

    def test_share_session_returns_404_for_missing_session(self) -> None:
        get_session = AsyncMock(side_effect=KeyError("missing"))

        with patch.object(main_module.manager, "get_session", get_session):
            response = self.client.post("/sessions/missing/share", json={"ttl_minutes": 15})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Unknown session")

    def test_shared_observe_returns_404_for_missing_shared_session(self) -> None:
        token_info = Mock(return_value={"valid": True, "session_id": "missing", "scope": SCOPE_OBSERVE})
        observe = AsyncMock(side_effect=KeyError("missing"))

        with patch.object(main_module.share_manager, "token_info", token_info), patch.object(
            main_module.manager, "observe", observe
        ):
            response = self.client.get("/share/fake-token/observe")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Unknown session")

    def test_shared_observe_rejects_invalid_token(self) -> None:
        token_info = Mock(return_value={"valid": False, "error": "invalid share token"})

        with patch.object(main_module.share_manager, "token_info", token_info):
            response = self.client.get("/share/fake-token/observe")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "invalid share token")

    def test_shared_session_page_renders_html_for_valid_token(self) -> None:
        token_info = Mock(return_value={"valid": True, "session_id": "session-1", "scope": SCOPE_OBSERVE})
        get_session = AsyncMock(return_value=SimpleNamespace(id="session-1"))

        with patch.object(main_module.share_manager, "token_info", token_info), patch.object(
            main_module.manager, "get_session", get_session
        ):
            response = self.client.get("/share/fake-token")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Shared Session Observer", response.text)
        self.assertIn('const token = window.location.pathname.split("/").filter(Boolean).pop() || "";', response.text)
        self.assertIn("const observeUrl = `/share/${token}/observe`;", response.text)

    def test_shared_session_page_returns_404_for_missing_session(self) -> None:
        token_info = Mock(return_value={"valid": True, "session_id": "missing", "scope": SCOPE_OBSERVE})
        get_session = AsyncMock(side_effect=KeyError("missing"))

        with patch.object(main_module.share_manager, "token_info", token_info), patch.object(
            main_module.manager, "get_session", get_session
        ):
            response = self.client.get("/share/fake-token")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Unknown session")

    def test_shared_session_page_rejects_invalid_token(self) -> None:
        token_info = Mock(return_value={"valid": False, "error": "invalid share token"})

        with patch.object(main_module.share_manager, "token_info", token_info):
            response = self.client.get("/share/fake-token")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "invalid share token")


if __name__ == "__main__":
    unittest.main()
