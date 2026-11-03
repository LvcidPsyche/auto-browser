from __future__ import annotations

import atexit
import base64
import hashlib
import os
import re
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

    def test_api_docs_are_served_without_a_token(self) -> None:
        # /docs is where the README sends people, in a browser that cannot send a
        # bearer token. The schema is public (it is in the repo) and holds no data.
        for path in ("/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get("/sessions").status_code, 401)

    def test_the_docs_pages_load_only_pinned_assets_with_integrity_hashes(self) -> None:
        # Swagger's Authorize box takes the API token, so every script and stylesheet the page loads from
        # another origin is an exact version with an SRI hash: a changed CDN file is refused, not run.
        for path in ("/docs", "/redoc"):
            with self.subTest(path=path):
                html = self.client.get(path).text
                tags = re.findall(r"<(?:script|link)\b[^>]*>", html)
                external = [tag for tag in tags if re.search(r'(?:src|href)="https?://', tag)]
                self.assertTrue(external, "the page loads its UI from a CDN")
                for tag in external:
                    self.assertRegex(tag, r'integrity="sha384-[A-Za-z0-9+/]{64}"')
                    self.assertIn('crossorigin="anonymous"', tag)
                    self.assertRegex(tag, r"@\d+\.\d+\.\d+/", "an exact version, not a floating major")
                self.assertNotIn("fonts.googleapis.com", html)
                self.assertIn("/openapi.json", html)

    def test_the_docs_pages_policy_lets_in_only_their_own_assets(self) -> None:
        # ReDoc fetched its footer logo from cdn.redoc.ly: the page's policy now admits scripts only by exact
        # URL (and Swagger's inline init by its hash), images and fonts only from here, and requests only here.
        for path in ("/docs", "/redoc"):
            with self.subTest(path=path):
                response = self.client.get(path)
                policy = dict(
                    part.strip().split(" ", 1)
                    for part in response.headers["Content-Security-Policy"].split(";")
                    if part.strip()
                )
                self.assertEqual(policy["default-src"], "'none'")
                self.assertEqual(policy["img-src"], "'self' data:")
                self.assertEqual(policy["connect-src"], "'self'")
                scripts = policy["script-src"].split()
                external = re.findall(r'<script src="([^"]+)"', response.text)
                self.assertEqual(sorted(s for s in scripts if s.startswith("https://")), sorted(external))
                self.assertNotIn("'unsafe-inline'", scripts)
                inline = re.findall(r"<script>(.*?)</script>", response.text, re.S)
                for body in inline:
                    digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
                    self.assertIn(f"'sha256-{digest}'", scripts)

    def test_a_changed_page_template_fails_loudly_rather_than_dropping_the_hash(self) -> None:
        from app.docs_pages import SWAGGER_JS, _with_integrity

        with self.assertRaisesRegex(RuntimeError, "no longer has exactly one"):
            _with_integrity('<script src="https://elsewhere.example/bundle.js"></script>', "src", SWAGGER_JS)

    def test_the_schema_declares_the_bearer_token_for_every_operation(self) -> None:
        # So Swagger's Authorize button sends it with "Try it out".
        schema = self.client.get("/openapi.json").json()

        self.assertEqual(schema["components"]["securitySchemes"]["bearerAuth"], {"type": "http", "scheme": "bearer"})
        self.assertEqual(schema["security"], [{"bearerAuth": []}])

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
