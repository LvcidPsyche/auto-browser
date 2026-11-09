"""GET /sessions/{id}/takeover-link checks the takeover token before it looks the session up (W2-19).

The app-wide `require_session_access` dependency ran `ensure_session_accessible` before the handler, so under named
API_BEARER_TOKENS an unknown id or another operator's session answered 404 before the takeover-token check, and that
refusal was never audited: a caller without the token could tell which ids exist. The route is now exempt from the
dependency; the handler refuses a malformed id (400), then a missing or wrong takeover token (403, audited), then
looks the session up (another operator's answers 404 as an unknown one does). Tested through create_controller_app,
which installs the dependency, with the real auth middleware.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.app_factory import build_controller_services, create_controller_app
from app.browser_manager import BrowserSession
from app.main import handle_key_not_found
from app.middleware.http import install_controller_http_middleware
from app.routes.sessions import create_sessions_router
from app.runtime_policy import MIN_PRODUCTION_BEARER_TOKEN_LENGTH
from app.utils import UTC
from tests.test_browser_manager_create_session import FakePage, _settings

ALICE_TOKEN = "alice-" + "a" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH
BOB_TOKEN = "bob-" + "b" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH
TAKEOVER = "takeover-sentinel-" + "t" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH


class _NoLifespan:
    def __init__(self, _app) -> None:
        pass

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc) -> None:
        return None


class TakeoverLinkOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        settings = _settings(root)
        settings.api_bearer_tokens = f"alice:{ALICE_TOKEN},bob:{BOB_TOKEN}"
        settings.takeover_token = TAKEOVER
        services = build_controller_services(settings, version="test")
        self.manager = services.manager
        self.manager.audit.append = AsyncMock()  # type: ignore[method-assign]
        application = create_controller_app(services=services, version="test", lifespan=_NoLifespan)
        application.include_router(create_sessions_router(manager=self.manager))
        application.add_exception_handler(KeyError, handle_key_not_found)
        install_controller_http_middleware(
            application, settings=settings, rate_limiter=None, metrics=SimpleNamespace(enabled=False)
        )
        for session_id, owner in (("owned-by-alice", "alice"), ("owned-by-bob", "bob")):
            artifact_dir = root / "artifacts" / session_id
            self.manager.sessions[session_id] = BrowserSession(
                id=session_id,
                name=session_id,
                created_at=datetime.now(UTC),
                context=object(),  # type: ignore[arg-type]
                page=FakePage(),  # type: ignore[arg-type]
                artifact_dir=artifact_dir,
                auth_dir=artifact_dir / "auth",
                upload_dir=artifact_dir / "uploads",
                takeover_url="http://127.0.0.1:6080/vnc.html",
                trace_path=artifact_dir / "trace.zip",
                owner=owner,
            )
        self.client = TestClient(application)

    def _get(self, session_id: str, takeover: str | None):
        headers = {"Authorization": f"Bearer {ALICE_TOKEN}"}
        if takeover is not None:
            headers["X-Takeover-Token"] = takeover
        return self.client.get(f"/sessions/{session_id}/takeover-link", headers=headers)

    @patch("app.routes.sessions.TAKEOVER_REFUSAL_WINDOW_SECONDS", 0)  # every refusal audits: this reads each event
    def test_without_the_token_an_unknown_and_another_operators_id_are_403_and_audited(self) -> None:
        for session_id in ("0a0a0a0a0a0a", "owned-by-bob", "owned-by-alice"):
            for takeover in (None, "wrong-" + TAKEOVER):
                with self.subTest(session_id=session_id, takeover=takeover):
                    self.manager.audit.append.reset_mock()
                    response = self._get(session_id, takeover)
                    self.assertEqual(response.status_code, 403, response.text)
                    events = [call.kwargs["event_type"] for call in self.manager.audit.append.await_args_list]
                    self.assertEqual(events, ["takeover_link_refused"])

    def test_each_verified_operator_has_its_own_refusal_window_from_one_host(self) -> None:
        # R5C-6: both come from the TestClient's one host, and a named credential is what tells them apart.
        self.manager.audit.append.reset_mock()
        for token in (ALICE_TOKEN, BOB_TOKEN, ALICE_TOKEN, BOB_TOKEN):
            response = self.client.get(
                "/sessions/owned-by-alice/takeover-link",
                headers={"Authorization": f"Bearer {token}", "X-Takeover-Token": "wrong"},
            )
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.manager.audit.append.await_count, 2)

    def test_with_the_token_an_unknown_and_another_operators_id_are_404_and_ones_own_is_200(self) -> None:
        self.assertEqual(self._get("0a0a0a0a0a0a", TAKEOVER).status_code, 404)
        self.assertEqual(self._get("owned-by-bob", TAKEOVER).status_code, 404)
        response = self._get("owned-by-alice", TAKEOVER)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_w2_22_the_refusal_names_only_the_header(self) -> None:
        # It told every caller without the token where the token lives (TAKEOVER_TOKEN, data/takeover-token).
        detail = self._get("owned-by-alice", None).json()["detail"]
        self.assertEqual(detail, "This endpoint needs the takeover token in the X-Takeover-Token header.")

    def test_w2_21_spaces_and_tabs_around_the_presented_token_are_not_part_of_it(self) -> None:
        # As normalize_authorization does for the bearer token: uvicorn's httptools keeps trailing whitespace.
        self.assertEqual(self._get("owned-by-alice", f" \t{TAKEOVER}\t ").status_code, 200)
        self.assertEqual(self._get("owned-by-alice", f"{TAKEOVER} x").status_code, 403)

    def test_a_malformed_id_is_400_with_a_fixed_text_before_the_token_check(self) -> None:
        self.manager.audit.append.reset_mock()
        response = self._get("..bad%20id", None)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Invalid session_id")
        self.manager.audit.append.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
