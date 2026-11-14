"""Behind a root_path, the controller's path rules read the path the router matches.

uvicorn puts ``--root-path`` in front of ``scope["path"]``, and the router matches what follows it. A rule that
compared the whole path missed there: a file under /artifacts was served without its sandbox CSP, and /healthz lost
what the token gate, the operator-id check, the rate limiter, the loopback Host check and the session check each
leave it. These requests carry the prefix as uvicorn's do: a TestClient with ``root_path``, and a URL that starts
with it.
"""

from __future__ import annotations

import shutil
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as main_module
from app.auth_policy import BIND_SCOPE_LOOPBACK
from app.middleware.http import _ARTIFACT_CSP, install_controller_http_middleware
from app.rate_limits import SlidingWindowRateLimiter

ROOT = "/api"
TOKEN = "t" * 40
RIGHT = {"Authorization": f"Bearer {TOKEN}"}
WRONG = {"Authorization": "Bearer " + "x" * 40}
DEFAULT_EXEMPT = ["/healthz", "/readyz", "/docs", "/openapi.json", "/redoc", "/artifacts", "/metrics"]


class TheControllerBehindARootPathTests(unittest.TestCase):
    """The whole app, with a token configured."""

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
        self.stack.enter_context(patch.object(main_module.settings, "api_bearer_token", TOKEN))
        self.stack.enter_context(patch.object(main_module.settings, "require_operator_id", False))
        self.session_dir = Path(main_module.settings.artifact_root) / f"rootpath-{uuid4().hex[:8]}"
        self.session_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.session_dir, True)
        (self.session_dir / "page.html").write_text("<script>fetch('/sessions')</script>", encoding="utf-8")
        self.artifact = f"{ROOT}/artifacts/{self.session_dir.name}/page.html"
        self.client = self.stack.enter_context(TestClient(main_module.app, root_path=ROOT))

    def tearDown(self) -> None:
        self.stack.close()

    def test_a_downloaded_html_file_is_still_sandboxed(self) -> None:
        response = self.client.get(self.artifact, headers=RIGHT)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-security-policy"), _ARTIFACT_CSP)

    def test_nothing_else_is_sandboxed(self) -> None:
        # The dashboard and the share pages run their own inline script.
        for path in (f"{ROOT}/healthz", f"{ROOT}/dashboard"):
            response = self.client.get(path, headers=RIGHT)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn("content-security-policy", response.headers, path)

    def test_healthz_still_needs_no_token(self) -> None:
        self.assertEqual(self.client.get(f"{ROOT}/healthz").status_code, 200)

    def test_the_other_open_paths_still_need_no_token(self) -> None:
        for path in (f"{ROOT}/version", f"{ROOT}/dashboard"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_healthz_still_needs_no_operator_id(self) -> None:
        with patch.object(main_module.settings, "require_operator_id", True):
            response = self.client.get(f"{ROOT}/healthz", headers=RIGHT)
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_session_id_on_healthz_still_tells_nothing(self) -> None:
        # W2-19: an exempt route names no session it could check, so an unknown id answers as a known one does. The
        # token gate and require_session_access must agree on what is exempt, or this is a 404 to a caller with no
        # token: which ids exist. Named credentials, under which an id with no session is refused.
        with patch.object(main_module.settings, "api_bearer_tokens", "alice:token-a"):
            response = self.client.get(f"{ROOT}/healthz", params={"session_id": "nope-1"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_what_needs_a_token_still_does(self) -> None:
        for path in (
            f"{ROOT}/sessions",
            f"{ROOT}/metrics",
            self.artifact,
            f"{ROOT}/healthz/",
            f"{ROOT}healthz",
            ROOT,
            f"{ROOT}/approvals?session_id=nope-1",
        ):
            for headers in ({}, WRONG):
                response = self.client.get(path, headers=headers, follow_redirects=False)
                self.assertEqual(
                    (response.status_code, response.json()),
                    (401, {"detail": "Missing or invalid bearer token"}),
                    (path, headers),
                )

    def test_what_needs_an_operator_id_still_does(self) -> None:
        # No /healthz/ here: this check exempts by prefix (/healthz/deep), with or without a root_path.
        with patch.object(main_module.settings, "require_operator_id", True):
            for path in (f"{ROOT}/sessions", f"{ROOT}healthz", ROOT):
                response = self.client.get(path, headers=RIGHT, follow_redirects=False)
                self.assertEqual(response.status_code, 400, path)
                self.assertIn("Missing required operator header", response.json()["detail"], path)

    def test_a_session_id_on_a_route_that_needs_the_token_is_still_checked(self) -> None:
        with patch.object(main_module.settings, "api_bearer_tokens", "alice:token-a"):
            response = self.client.get(
                f"{ROOT}/approvals", params={"session_id": "nope-1"}, headers={"Authorization": "Bearer token-a"}
            )
        self.assertEqual((response.status_code, response.json().get("code")), (404, "unknown_session"), response.text)


def _application(*, token: str | None, exempt: list[str], rate_limiter: object | None = None) -> FastAPI:
    application = FastAPI()
    settings = SimpleNamespace(
        api_bearer_token=token,
        api_allow_tokenless_loopback=token is None,
        api_bind_scope=BIND_SCOPE_LOOPBACK,
        model_fields_set=frozenset({"api_bind_scope"}),
        request_rate_limit_exempt_path_list=exempt,
        operator_id_header="X-Operator-Id",
        operator_name_header="X-Operator-Name",
        require_operator_id=False,
    )

    @application.get("/healthz")
    @application.get("/metrics")
    @application.get("/sessions")
    @application.get("/artifacts/{session_id}/{name}")
    async def served() -> dict[str, bool]:
        return {"ok": True}

    install_controller_http_middleware(
        application, settings=settings, rate_limiter=rate_limiter, metrics=SimpleNamespace(enabled=False)
    )
    return application


class TheRateLimiterBehindARootPathTests(unittest.TestCase):
    """Which requests the limiter is asked about: REQUEST_RATE_LIMIT_EXEMPT_PATHS names paths as the router does."""

    def counted(self, path: str, headers: dict[str, str], *, exempt: list[str] = DEFAULT_EXEMPT) -> tuple[int, bool]:
        limiter = SlidingWindowRateLimiter(limit=1000, window_seconds=60)
        asked = AsyncMock(wraps=limiter.evaluate)
        with patch.object(limiter, "evaluate", asked):
            client = TestClient(_application(token=TOKEN, exempt=exempt, rate_limiter=limiter), root_path=ROOT)
            status = client.get(path, headers=headers).status_code
        return status, asked.await_count > 0

    def test_a_healthcheck_is_not_counted(self) -> None:
        self.assertEqual(self.counted(f"{ROOT}/healthz", {}), (200, False))

    def test_an_exempt_path_asked_for_with_the_token_is_not_counted(self) -> None:
        for path in (f"{ROOT}/artifacts/s1/page.html", f"{ROOT}/metrics"):
            self.assertEqual(self.counted(path, RIGHT), (200, False), path)

    def test_a_guess_at_an_exempt_path_that_needs_the_token_is_still_counted(self) -> None:
        for path in (f"{ROOT}/artifacts/s1/page.html", f"{ROOT}/metrics"):
            for headers in ({}, WRONG):
                self.assertEqual(self.counted(path, headers), (401, True), (path, headers))

    def test_every_other_path_is_still_counted(self) -> None:
        for path in (f"{ROOT}/sessions", f"{ROOT}/healthz/", f"{ROOT}healthz", ROOT):
            self.assertTrue(self.counted(path, RIGHT)[1], path)
            self.assertEqual(self.counted(path, {}), (401, True), path)

    def test_the_list_is_compared_with_the_path_the_router_matches(self) -> None:
        # The upgrade note: an entry written with the prefix in front (a workaround for the bug) names no path now.
        artifact = f"{ROOT}/artifacts/s1/page.html"
        self.assertEqual(self.counted(artifact, RIGHT, exempt=["/artifacts"]), (200, False))
        self.assertEqual(self.counted(artifact, RIGHT, exempt=[f"{ROOT}/artifacts"]), (200, True))
        self.assertEqual(self.counted(f"{ROOT}/healthz", RIGHT, exempt=[f"{ROOT}/healthz"]), (200, True))


class ATokenlessControllerBehindARootPathTests(unittest.TestCase):
    """No token and no CONTROLLER_ALLOWED_HOSTS: only a loopback Host is answered, and /healthz under any name."""

    REBOUND = {"host": "rebound.example"}

    def setUp(self) -> None:
        self.client = TestClient(_application(token=None, exempt=[]), base_url="http://127.0.0.1:8000", root_path=ROOT)

    def test_healthz_is_still_answered_under_any_host(self) -> None:
        self.assertEqual(self.client.get(f"{ROOT}/healthz", headers=self.REBOUND).status_code, 200)

    def test_everything_else_still_answers_a_loopback_host_only(self) -> None:
        for path in (f"{ROOT}/sessions", f"{ROOT}/healthz/", f"{ROOT}healthz", ROOT, f"{ROOT}/artifacts/s1/page.html"):
            response = self.client.get(path, headers=self.REBOUND, follow_redirects=False)
            self.assertEqual(response.status_code, 400, path)
            self.assertIn("Invalid host header", response.json()["detail"], path)
        self.assertEqual(self.client.get(f"{ROOT}/sessions").status_code, 200)


if __name__ == "__main__":
    unittest.main()
