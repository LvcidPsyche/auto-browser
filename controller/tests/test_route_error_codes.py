"""Typed errors reach REST clients as their own status, not as a 500 "Internal error".

Most routes under app/routes/ end in ``except Exception: raise internal_error(...)``.
That catch-all ran before the app-level handlers in app/main.py, so an unknown
session, a ``BrowserActionError`` (a timeout, a missing element) and an error
the route forgot to map all came back as 500 "Internal error" and hid the
actionable ``code`` from agents.

These tests go through the real ``app.main.app`` and the real route wiring; only
the manager call under test is replaced.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.action_errors import BrowserActionError, SessionNotFoundError
from app.routes._utils import OFF_LIST_PAGE_DETAIL

SID = "s-missing"


@dataclass(frozen=True)
class Route:
    """One REST route that wraps a manager call in a broad ``except Exception``."""

    verb: str
    path: str
    body: dict | None
    method: str  # the manager attribute the route awaits
    # Route-specific answers that exist on purpose and must survive the fix.
    permission: tuple[int, str] = (403, "Not permitted")
    runtime: tuple[int, str] = (500, "Internal error")

    @property
    def id(self) -> str:
        return f"{self.verb.upper()} {self.path}"


_ELEMENT = {"selector": "#a"}
SESSION_ROUTES = [
    Route("get", f"/sessions/{SID}/observe", None, "observe", permission=(403, OFF_LIST_PAGE_DETAIL)),
    Route("post", f"/sessions/{SID}/observe", {}, "observe", permission=(403, OFF_LIST_PAGE_DETAIL)),
    Route(
        "post", f"/sessions/{SID}/screenshot/compare", None, "screenshot_diff", permission=(403, OFF_LIST_PAGE_DETAIL)
    ),
    Route("post", f"/sessions/{SID}/actions/navigate", {"url": "https://example.com"}, "navigate"),
    Route("post", f"/sessions/{SID}/actions/click", _ELEMENT, "click"),
    Route("post", f"/sessions/{SID}/actions/type", {**_ELEMENT, "text": "x"}, "type"),
    Route("post", f"/sessions/{SID}/actions/press", {"key": "Enter"}, "press"),
    Route("post", f"/sessions/{SID}/actions/scroll", {"delta_x": 0, "delta_y": 100}, "scroll"),
    Route(
        "post",
        f"/sessions/{SID}/actions/execute",
        {"action": {"action": "click", "selector": "#a", "reason": "r"}},
        "execute_decision",
    ),
    Route("post", f"/sessions/{SID}/actions/upload", {"selector": "#f", "file_path": "a.txt"}, "upload"),
    Route("post", f"/sessions/{SID}/actions/hover", _ELEMENT, "hover"),
    Route("post", f"/sessions/{SID}/actions/select-option", {"selector": "#s", "value": "1"}, "select_option"),
    Route("post", f"/sessions/{SID}/actions/wait", {"wait_ms": 10}, "wait"),
    Route("post", f"/sessions/{SID}/actions/reload", None, "reload"),
    Route("post", f"/sessions/{SID}/actions/go-back", None, "go_back"),
    Route("post", f"/sessions/{SID}/actions/go-forward", None, "go_forward"),
]

# Routes with a broad catch-all that are not addressed by a session id.
OTHER_ROUTES = [
    # create_session answers RuntimeError 409 on purpose (a session cap, a busy browser).
    Route("post", "/sessions", {}, "create_session", runtime=(409, "Conflict")),
    # An approval that is not approved is a state conflict, not a refusal.
    Route("post", "/approvals/a-1/execute", None, "execute_approval", permission=(409, "Conflict")),
    Route("get", "/auth-profiles/p1/export", None, "export_auth_profile"),
    Route("post", "/auth-profiles/import", {"archive_path": "a.tgz"}, "import_auth_profile"),
]

ALL_ROUTES = SESSION_ROUTES + OTHER_ROUTES


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[]))
        )
        for service, name in (
            (main_module.manager, "startup"),
            (main_module.manager, "shutdown"),
            (main_module.job_queue, "startup"),
            (main_module.job_queue, "shutdown"),
            (main_module.cron_service, "startup"),
            (main_module.cron_service, "shutdown"),
            (main_module.maintenance, "startup"),
            (main_module.maintenance, "shutdown"),
        ):
            stack.enter_context(patch.object(service, name, new=AsyncMock()))
        # raise_server_exceptions=False: a handler gap must show up as the 500 a
        # real client would see, not as an exception inside the test.
        yield stack.enter_context(TestClient(main_module.app, raise_server_exceptions=False))


@pytest.fixture(autouse=True)
def _fresh_rate_limit() -> None:
    # 120 requests a minute per client; this module sends more than that.
    if main_module.rate_limiter is not None:
        main_module.rate_limiter._events.clear()


def _call(client: TestClient, route: Route):
    kwargs = {"json": route.body} if route.body is not None else {}
    return getattr(client, route.verb)(route.path, **kwargs)


def _raising(route: Route, exc: BaseException):
    return patch.object(main_module.manager, route.method, new=AsyncMock(side_effect=exc))


@pytest.mark.parametrize("route", SESSION_ROUTES, ids=lambda r: r.id)
def test_unknown_session_is_404_with_its_code(client: TestClient, route: Route) -> None:
    # No patching: the real manager is asked for a session that does not exist.
    response = _call(client, route)
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["code"] == "unknown_session"
    assert body["detail"] == SessionNotFoundError(SID).message


@pytest.mark.parametrize("route", SESSION_ROUTES, ids=lambda r: r.id)
def test_closed_session_is_404_and_says_it_is_closed(client: TestClient, route: Route) -> None:
    exc = SessionNotFoundError(SID, status="closed")
    with _raising(route, exc):
        response = _call(client, route)
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": exc.message, "code": "session_closed"}


@pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: r.id)
def test_browser_action_error_keeps_its_status_and_payload(client: TestClient, route: Route) -> None:
    exc = BrowserActionError(
        "Timed out waiting for #a",
        code="action_timeout",
        action="click",
        status_code=408,
        retryable=True,
    )
    with _raising(route, exc):
        response = _call(client, route)
    assert response.status_code == 408, response.text
    assert response.json() == exc.payload


@pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: r.id)
def test_permission_error_is_a_generic_refusal(client: TestClient, route: Route) -> None:
    status, detail = route.permission
    with _raising(route, PermissionError("Host 'evil.example' is not allowlisted")):
        response = _call(client, route)
    assert response.status_code == status, response.text
    # A fixed text: the exception's own message can carry hosts and paths.
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: r.id)
def test_value_error_is_400_without_its_message(client: TestClient, route: Route) -> None:
    with _raising(route, ValueError("bad value at /srv/secret/path")):
        response = _call(client, route)
    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "Invalid request"}


@pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: r.id)
def test_file_not_found_is_404_without_its_message(client: TestClient, route: Route) -> None:
    with _raising(route, FileNotFoundError("/srv/secret/path is missing")):
        response = _call(client, route)
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Not found"}


@pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: r.id)
def test_unexpected_error_is_logged_and_answers_generically(
    client: TestClient, route: Route, caplog: pytest.LogCaptureFixture
) -> None:
    status, detail = route.runtime
    with caplog.at_level(logging.ERROR), _raising(route, RuntimeError("boom at /srv/secret/path")):
        response = _call(client, route)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}
    if status == 500:
        # The log line is the only place the cause survives.
        logged = [r for r in caplog.records if r.name.startswith("app.routes") and r.exc_info]
        assert logged, "the route answered 500 without logging the exception"
        assert logged[0].exc_info[0] is RuntimeError


def test_unknown_approval_is_404(client: TestClient) -> None:
    # execute_approval raises a plain KeyError(approval_id); GET /approvals/{id}
    # already answers that 404 through the app handler.
    response = client.post("/approvals/nope/execute")
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Not found: nope"}


def test_import_conflict_stays_409(client: TestClient) -> None:
    route = OTHER_ROUTES[3]
    with _raising(route, FileExistsError("profile 'p1' already exists")):
        response = _call(client, route)
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "Conflict"}
