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
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.action_errors import BrowserActionError, BrowserUnreachableError, SessionNotFoundError
from app.approvals import ApprovalRequiredError
from app.auth_state import AuthStateDecryptError
from app.browser.services.session_creation import SessionLimitError
from app.browser_manager import BrowserManager
from app.config import Settings
from app.models import ApprovalRecord, BrowserActionDecision
from app.routes._utils import OFF_LIST_PAGE_DETAIL, OFF_LIST_TAB_DETAIL

SID = "s-missing"


@dataclass(frozen=True)
class Route:
    """One REST route that wraps a manager call in a broad ``except Exception``."""

    verb: str
    path: str
    body: dict | None
    method: str  # the attribute the route calls, on the manager unless `owner` names another service of app.main
    # Route-specific answers that exist on purpose and must survive the fix.
    permission: tuple[int, str] = (403, "Not permitted")
    runtime: tuple[int, str] = (500, "Internal error")
    owner: str = "manager"
    sync: bool = False  # the route calls `method` without awaiting it
    # False for a route that answers a ValueError with its own text, on purpose (a job's state refusal).
    value_error_is_generic: bool = True

    @property
    def id(self) -> str:
        return f"{self.verb.upper()} {self.path}"


_ELEMENT = {"selector": "#a"}
_AGENT_BODY = {"provider": "openai", "goal": "inspect the page"}
_AGENT_DOWN = (503, "Service unavailable")
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
    Route("get", f"/sessions/{SID}/downloads", None, "list_downloads"),
    Route("get", f"/sessions/{SID}/tabs", None, "list_tabs"),
    Route("post", f"/sessions/{SID}/takeover", {"reason": "check the page"}, "request_human_takeover"),
    Route("delete", f"/sessions/{SID}", None, "close_session"),
    # A RuntimeError from fork_session is a state conflict on purpose. Only the refused page's own refusal
    # answers OFF_LIST_PAGE_DETAIL (test_a_forks_page_refusal_answers_as_the_page_routes_do); any other
    # PermissionError, create_session's URL check among them, is the generic refusal (R4C-L1-5).
    Route("post", f"/sessions/{SID}/fork", None, "fork_session", runtime=(409, "Conflict")),
    Route(
        "post", f"/sessions/{SID}/fork?start_url=https://example.com/", None, "fork_session", runtime=(409, "Conflict")
    ),
    Route("post", f"/sessions/{SID}/storage-state", {"path": "state.json"}, "save_storage_state"),
    Route("get", f"/sessions/{SID}", None, "get_session_record"),
    Route("post", f"/sessions/{SID}/screenshot", {}, "capture_screenshot", permission=(403, OFF_LIST_PAGE_DETAIL)),
    Route(
        "post", f"/sessions/{SID}/tabs/activate", {"index": 0}, "activate_tab", permission=(403, OFF_LIST_TAB_DETAIL)
    ),
    Route("post", f"/sessions/{SID}/tabs/close", {"index": 0}, "close_tab"),
    Route("post", f"/sessions/{SID}/tabs/open", {"url": "https://example.com/"}, "open_tab"),
    Route("post", f"/sessions/{SID}/auth-profiles", {"profile_name": "p1"}, "save_auth_profile"),
]

# Records of a session: they outlive it, so an id that never existed answers an
# empty 200 rather than a 404. The error mapping is the same as for live sessions.
RECORD_ROUTES = [
    # The manager's audit log is a service object: "audit.list" is manager.audit.list.
    Route("get", f"/sessions/{SID}/audit", None, "audit.list"),
    Route("get", f"/sessions/{SID}/witness", None, "list_witness_receipts"),
    Route("get", f"/sessions/{SID}/witness/verify", None, "verify_witness_chain"),
    Route("get", f"/sessions/{SID}/witness/bundle", None, "export_witness_bundle"),
]

# Routes with a broad catch-all that are not addressed by a session id.
OTHER_ROUTES = [
    # create_session answers RuntimeError 409 on purpose (a session cap, a busy browser).
    Route("post", "/sessions", {}, "create_session", runtime=(409, "Conflict")),
    # An approval that is not approved is a state conflict, not a refusal.
    Route("post", "/approvals/a-1/execute", None, "execute_approval", permission=(409, "Conflict")),
    Route("get", "/auth-profiles/p1/export", None, "export_auth_profile"),
    Route("post", "/auth-profiles/import", {"archive_path": "a.tgz"}, "import_auth_profile"),
    Route("get", "/sessions", None, "list_sessions"),
    # An unknown session is a plain 404 here, the session store's KeyError, not the coded one the session routes give.
    Route("get", f"/sessions/{SID}/auth-state", None, "get_auth_state_info"),
    Route("get", "/auth-profiles/p1", None, "get_auth_profile"),
    Route("get", "/remote-access", None, "get_remote_access_info", sync=True),
    Route("get", "/audit/events", None, "list_audit_events"),
    Route("get", "/approvals", None, "list_approvals"),
    Route("get", "/approvals/a-1", None, "get_approval"),
    Route("get", "/approvals/a-1/arguments", None, "approval_arguments"),
    Route("get", "/pii-scrubber", None, "get_pii_scrubber_status", sync=True),
    # The agent routes keep their own answers: an unknown session (the orchestrator's KeyError) is 404 "Unknown
    # session", and a RuntimeError from a run is 503. The enqueue routes check the session first, so that call stands
    # in for the route.
    Route("get", "/agent/jobs", None, "list_jobs", owner="job_queue"),
    Route("get", "/agent/jobs/j-1", None, "get_job", owner="job_queue"),
    Route("post", f"/sessions/{SID}/agent/step", _AGENT_BODY, "step", owner="orchestrator", runtime=_AGENT_DOWN),
    Route("post", f"/sessions/{SID}/agent/run", _AGENT_BODY, "run", owner="orchestrator", runtime=_AGENT_DOWN),
    Route("post", f"/sessions/{SID}/agent/jobs/step", _AGENT_BODY, "get_session"),
    Route("post", f"/sessions/{SID}/agent/jobs/run", _AGENT_BODY, "get_session"),
    Route(
        "post",
        "/agent/jobs/j-1/resume",
        None,
        "resume_job",
        owner="job_queue",
        runtime=_AGENT_DOWN,
        value_error_is_generic=False,
    ),
    Route("post", "/agent/jobs/j-1/discard", None, "discard_job", owner="job_queue", value_error_is_generic=False),
    Route("post", "/agent/jobs/j-1/cancel", None, "cancel_job", owner="job_queue", value_error_is_generic=False),
]

ALL_ROUTES = SESSION_ROUTES + RECORD_ROUTES + OTHER_ROUTES


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
    *owners, name = route.method.split(".")
    target = getattr(main_module, route.owner)
    for owner in owners:
        target = getattr(target, owner)
    return patch.object(target, name, new=(Mock if route.sync else AsyncMock)(side_effect=exc))


@pytest.mark.parametrize("route", SESSION_ROUTES, ids=lambda r: r.id)
def test_unknown_session_is_404_with_its_code(client: TestClient, route: Route) -> None:
    # No patching: the real manager is asked for a session that does not exist.
    response = _call(client, route)
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["code"] == "unknown_session"
    assert body["detail"] == SessionNotFoundError(SID).message


@pytest.mark.parametrize("route", SESSION_ROUTES + RECORD_ROUTES, ids=lambda r: r.id)
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
def test_approval_required_reaches_the_caller_with_its_approval_id(client: TestClient, route: Route) -> None:
    # Each route used to re-raise ApprovalRequiredError by name; it now passes
    # through as an HTTPException. The approval_id is what the caller hands back
    # once an operator approves, so losing it to a 500 strands the agent.
    approval = ApprovalRecord(
        id="approval-1",
        session_id=SID,
        kind="write",
        status="pending",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        reason="needs review",
        action=BrowserActionDecision(action="click", reason="click", selector="#a", risk_category="write"),
    )
    exc = ApprovalRequiredError(approval)
    with _raising(route, exc):
        response = _call(client, route)
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": exc.payload}


@pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: r.id)
def test_permission_error_is_a_generic_refusal(client: TestClient, route: Route) -> None:
    status, detail = route.permission
    with _raising(route, PermissionError("Host 'evil.example' is not allowlisted")):
        response = _call(client, route)
    assert response.status_code == status, response.text
    # A fixed text: the exception's own message can carry hosts and paths.
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize("start_url", [None, "https://example.com/"], ids=["page-url", "start-url"])
def test_a_forks_page_refusal_answers_as_the_page_routes_do(client: TestClient, start_url: str | None) -> None:
    """F4: the fork of a refused page opens its URL with its cookies; its refusal says why, as observe's does."""
    manager = BrowserManager(Settings(_env_file=None, ALLOWED_HOSTS="allowed.example"))
    with pytest.raises(PermissionError) as refused:
        manager._assert_page_object_allowed(SimpleNamespace(url="https://refused.example/"))
    page_refusal = refused.value
    params = {"start_url": start_url} if start_url else {}
    with patch.object(main_module.manager, "fork_session", new=AsyncMock(side_effect=page_refusal)):
        response = client.post(f"/sessions/{SID}/fork", params=params)
    assert (response.status_code, response.json()) == (403, {"detail": OFF_LIST_PAGE_DETAIL})


@pytest.mark.parametrize("route", [r for r in ALL_ROUTES if r.runtime == (409, "Conflict")], ids=lambda r: r.id)
def test_a_runtime_error_answered_409_is_logged_with_its_cause(
    client: TestClient, route: Route, caplog: pytest.LogCaptureFixture
) -> None:
    # A tested 409 keeps its answer; only the log line is the operator's way to the cause.
    with caplog.at_level(logging.WARNING, logger="app.routes.sessions"), _raising(route, RuntimeError("boom-cause")):
        response = _call(client, route)
    assert (response.status_code, response.json()) == (409, {"detail": "Conflict"})
    logged = [r for r in caplog.records if r.name == "app.routes.sessions" and r.exc_info]
    assert logged, "the route answered 409 without logging the RuntimeError"
    assert logged[0].exc_info[0] is RuntimeError
    assert logged[0].levelno == logging.WARNING
    assert "boom-cause" in str(logged[0].exc_info[1])


@pytest.mark.parametrize("route", [r for r in ALL_ROUTES if r.value_error_is_generic], ids=lambda r: r.id)
def test_value_error_is_400_without_its_message(client: TestClient, route: Route) -> None:
    with _raising(route, ValueError("bad value at /srv/secret/path")):
        response = _call(client, route)
    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "Invalid request"}


# The routes whose manager call can reach a browser connect: Chrome over CDP_CONNECT_URL, the shared browser-node, or
# an isolated session's own browser (connect_browser, under acquire_session_browser). A failed connect raises
# BrowserUnreachableError, which is not a RuntimeError, so their "Conflict" for one does not answer it.
BROWSER_ROUTES = [route for route in ALL_ROUTES if route.method in ("create_session", "fork_session")]


@pytest.mark.parametrize("route", BROWSER_ROUTES, ids=lambda r: r.id)
def test_an_unreachable_browser_is_503_with_a_fixed_detail(client: TestClient, route: Route) -> None:
    reason = "could not connect to Chrome over CDP at ws://10.0.0.9:9222: WebSocket error: connect ECONNREFUSED"
    with _raising(route, BrowserUnreachableError(reason)):
        response = _call(client, route)
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "the browser could not be reached"}


class _UnreachableChromium:
    async def connect(self, ws_target: str):
        raise ConnectionRefusedError(f"connect ECONNREFUSED {ws_target}")


def _shared_node_unreachable() -> ExitStack:
    """The real path from create_session to connect_browser, with a shared browser-node that never answers."""
    stack = ExitStack()
    manager = main_module.manager
    stack.enter_context(patch.object(manager, "playwright", SimpleNamespace(chromium=_UnreachableChromium())))
    stack.enter_context(patch.object(manager, "browser", None))
    stack.enter_context(patch.object(manager.settings, "connect_retries", 1))
    stack.enter_context(patch.object(manager.settings, "connect_retry_delay_seconds", 0))
    stack.enter_context(patch.object(manager.settings, "cdp_connect_url", None))
    stack.enter_context(patch.object(manager.settings, "session_isolation_mode", "shared_browser_node"))
    stack.enter_context(patch.object(manager.settings, "browser_ws_endpoint", "ws://browser-node:9223/sentinel-path"))
    return stack


def test_an_unreachable_shared_browser_node_is_503_on_create(client: TestClient) -> None:
    # R-2: connect_browser raised a RuntimeError, which POST /sessions answers as a 409 Conflict.
    with _shared_node_unreachable():
        response = client.post("/sessions", json={})
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "the browser could not be reached"}


def test_an_unreachable_shared_browser_node_is_503_on_fork(client: TestClient) -> None:
    # CB2-2: the real fork path, from the route through fork_session and create_session to connect_browser. The
    # source session is made on a fake browser; the fork's own create then connects and meets the unreachable node.
    from tests.test_auth_state_ownership import StateContext
    from tests.test_browser_manager_create_session import FakeBrowser, FakePage

    manager = main_module.manager
    source = FakeBrowser(StateContext(FakePage()))
    with (
        patch.object(manager, "_acquire_session_browser", new=AsyncMock(return_value=(source, None))),
        patch.object(manager, "_settle", new=AsyncMock()),
        patch.object(manager, "_maybe_provision_session_tunnel", new=AsyncMock()),
    ):
        source_id = client.post("/sessions", json={"name": "fork-source"}).json()["id"]
    try:
        connect = AsyncMock(side_effect=ConnectionRefusedError("connect ECONNREFUSED"))
        with (
            _shared_node_unreachable(),
            patch.object(manager.playwright.chromium, "connect", new=connect),
            patch.object(manager.settings, "max_sessions", 2),  # a fork counts as a session
        ):
            response = client.post(f"/sessions/{source_id}/fork")
        connect.assert_awaited()  # the fork's own session reached the connect
    finally:
        manager.sessions.pop(source_id, None)
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "the browser could not be reached"}


def test_an_unreachable_isolated_browser_is_503_and_its_container_is_released(client: TestClient) -> None:
    # CB2-2: the docker_ephemeral branch of acquire_session_browser: the session's own browser does not answer.
    manager = main_module.manager
    runtime = SimpleNamespace(
        ws_endpoint="ws://browser-session-x:9223/sentinel-path", ws_endpoint_file="/data/browser-sessions/x/profile"
    )
    provisioner = SimpleNamespace(provision=AsyncMock(return_value=runtime), release=AsyncMock())
    with (
        _shared_node_unreachable(),
        patch.object(manager.settings, "session_isolation_mode", "docker_ephemeral"),
        patch.object(manager, "runtime_provisioner", provisioner),
    ):
        response = client.post("/sessions", json={})
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "the browser could not be reached"}
    provisioner.release.assert_awaited_once_with(runtime)


def test_a_sandbox_refusal_of_the_shared_browser_node_stays_409(client: TestClient) -> None:
    manager = main_module.manager
    with (
        patch.object(manager.runtime, "ensure_browser", new=AsyncMock(return_value=SimpleNamespace())),
        patch.object(manager.runtime, "shared_sandbox", None),
        patch.object(manager.settings, "browser_sandbox", "on"),
        patch.object(manager.settings, "cdp_connect_url", None),
        patch.object(manager.settings, "session_isolation_mode", "shared_browser_node"),
    ):
        response = client.post("/sessions", json={})
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "Conflict"}


def test_a_cron_trigger_that_meets_an_unreachable_browser_is_503_with_a_fixed_detail(client: TestClient) -> None:
    # R-1: the trigger creates the run's session (create_session, then ensure_browser) and mapped no such error.
    reason = "could not connect to Chrome over CDP at ws://10.0.0.9:9222: WebSocket error: connect ECONNREFUSED"
    with patch.object(
        main_module.cron_service, "trigger_via_webhook", new=AsyncMock(side_effect=BrowserUnreachableError(reason))
    ):
        response = client.post("/crons/job-1/trigger", json={"webhook_key": "sentinel-key"})
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "the browser could not be reached"}


def test_a_cron_trigger_at_the_session_cap_is_409_with_a_fixed_detail_naming_no_session(client: TestClient) -> None:
    # CB1-8: SessionLimitError (a RuntimeError) from the run's create_session was a bare 500 here. W2-14: its text
    # names the job owner's live session ids (check_limit runs as the owner), and the trigger checks only the
    # webhook key, so another operator holding the key read them: the trigger answers a fixed detail instead.
    from app.routes.operations import CRON_TRIGGER_SESSION_LIMIT_DETAIL

    error = SessionLimitError(
        "Session limit reached: max_sessions=1. Active live session(s): sentinel-owner-session-id."
    )
    with patch.object(main_module.cron_service, "trigger_via_webhook", new=AsyncMock(side_effect=error)):
        response = client.post("/crons/job-1/trigger", json={"webhook_key": "sentinel-key"})
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": CRON_TRIGGER_SESSION_LIMIT_DETAIL}
    assert "sentinel-owner-session-id" not in response.text


def test_a_cron_trigger_whose_auth_state_cannot_be_decrypted_is_422_as_post_sessions_is(client: TestClient) -> None:
    error = AuthStateDecryptError("the saved auth state could not be decrypted: check AUTH_STATE_ENCRYPTION_KEY")
    with patch.object(main_module.cron_service, "trigger_via_webhook", new=AsyncMock(side_effect=error)):
        response = client.post("/crons/job-1/trigger", json={"webhook_key": "sentinel-key"})
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": str(error)}


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


def test_unmapped_key_error_is_404_and_logged(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    # A plain KeyError from a bug looks the same to the client as an unknown id,
    # so the handler is the only place it can be told apart.
    route = SESSION_ROUTES[0]
    with caplog.at_level(logging.WARNING, logger="app.main"), _raising(route, KeyError("internal_dict_key")):
        response = _call(client, route)
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Not found: internal_dict_key"}
    logged = [r for r in caplog.records if r.name == "app.main" and r.levelno == logging.WARNING]
    assert logged, "an unmapped KeyError was answered 404 without a log line"
    assert "unmapped KeyError" in logged[0].getMessage()
    assert logged[0].exc_info and logged[0].exc_info[0] is KeyError


def test_unknown_session_is_not_logged_as_an_unmapped_key_error(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.main"):
        response = _call(client, SESSION_ROUTES[0])
    assert response.status_code == 404, response.text
    assert [r for r in caplog.records if r.name == "app.main"] == []


def test_import_conflict_stays_409(client: TestClient) -> None:
    route = OTHER_ROUTES[3]
    with _raising(route, FileExistsError("profile 'p1' already exists")):
        response = _call(client, route)
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "Conflict"}
