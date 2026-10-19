from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="auto-browser-main-auth-"))
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
from app.middleware.http import install_controller_http_middleware


class MainAuthTests(unittest.TestCase):
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
        self.stack.enter_context(patch.object(main_module.settings, "api_bearer_token", "secret"))
        self.client = self.stack.enter_context(TestClient(main_module.app))

    def tearDown(self) -> None:
        self.stack.close()

    def test_healthz_is_exempt_from_bearer_auth(self) -> None:
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_version_is_exempt_from_bearer_auth(self) -> None:
        response = self.client.get("/version")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"version": main_module._VERSION})

    def test_deep_health_is_not_exempt_from_bearer_auth(self) -> None:
        response = self.client.get("/healthz/deep")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    def test_missing_or_invalid_bearer_token_returns_401(self) -> None:
        response = self.client.get("/sessions")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    def test_crafted_host_header_cannot_bypass_bearer_auth(self) -> None:
        app = FastAPI()
        metrics = SimpleNamespace(enabled=False)
        settings = SimpleNamespace(
            api_bearer_token="secret",
            request_rate_limit_exempt_path_list=[],
            operator_id_header="X-Operator-Id",
            operator_name_header="X-Operator-Name",
            require_operator_id=False,
        )
        install_controller_http_middleware(app, settings=settings, rate_limiter=None, metrics=metrics)

        @app.get("/sessions")
        async def protected() -> dict[str, bool]:
            return {"ok": True}

        with TestClient(app) as client:
            response = client.get("/sessions", headers={"Host": "example.com/healthz?x="})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    def test_valid_bearer_token_allows_request(self) -> None:
        with patch.object(main_module.manager, "list_sessions", new=AsyncMock(return_value=[{"id": "session-1"}])):
            response = self.client.get("/sessions", headers={"authorization": "Bearer secret"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"id": "session-1"}])

    def test_dashboard_bootstrap_page_is_exempt_from_initial_auth_headers(self) -> None:
        with patch.object(main_module.settings, "require_operator_id", True):
            response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(main_module.settings.operator_id_header, response.text)
        self.assertIn(main_module.settings.operator_name_header, response.text)

    def test_dashboard_api_subpath_is_not_exempt_from_bearer_auth(self) -> None:
        with patch.object(main_module.settings, "require_operator_id", True):
            response = self.client.get("/dashboard/api")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    def test_legacy_ui_redirects_to_dashboard_bootstrap(self) -> None:
        with patch.object(main_module.settings, "require_operator_id", True):
            response = self.client.get("/ui/", follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/dashboard")

    def test_version_does_not_need_an_operator_id(self) -> None:
        # A version probe has no operator; /healthz and /readyz are exempt for the same reason.
        with patch.object(main_module.settings, "require_operator_id", True):
            response = self.client.get("/version")

        self.assertEqual(response.status_code, 200)

    def test_legacy_ui_subpaths_redirect_like_ui(self) -> None:
        with patch.object(main_module.settings, "require_operator_id", True):
            response = self.client.get("/ui/foo", follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/dashboard")

    def test_ui_lookalike_paths_still_need_the_bearer_token(self) -> None:
        for path in ("/uifoo", "/ui2/x"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, follow_redirects=False).status_code, 401)

    def test_mesh_receive_is_exempt_from_bootstrap_auth(self) -> None:
        body = {
            "sender_node_id": "peer-1",
            "recipient_node_id": "node-1",
            "nonce": "nonce-1",
            "timestamp": 1.0,
            "payload": {},
            "signature_b64": "ZmFrZQ==",
        }
        with patch.object(main_module.settings, "require_operator_id", True):
            response = self.client.post("/mesh/receive", json=body)

        self.assertEqual(response.status_code, 503)

    def test_controller_allowed_hosts_rejects_unexpected_host_headers(self) -> None:
        app = FastAPI()

        @app.get("/")
        async def root() -> dict[str, bool]:
            return {"ok": True}

        main_module._install_controller_host_middleware(app, ["controller.example.com"])

        with TestClient(app) as client:
            allowed = client.get("/", headers={"host": "controller.example.com"})
            rejected = client.get("/", headers={"host": "evil.example.com"})

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(rejected.status_code, 400)
