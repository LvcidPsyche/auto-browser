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

import errno
import logging
import re
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import app.main as main_module
from app.action_errors import BrowserActionError, BrowserUnreachableError, SessionNotFoundError
from app.approvals import ApprovalRequiredError
from app.audit import FileAuditStore
from app.auth_state import AuthStateDecryptError
from app.browser.services.session_creation import SessionLimitError
from app.browser_manager import BrowserManager
from app.config import Settings
from app.models import ApprovalRecord, BrowserActionDecision
from app.routes._utils import OFF_LIST_PAGE_DETAIL, OFF_LIST_TAB_DETAIL, typed_route_errors
from app.routes.operations import CRON_TRIGGER_RUN_REFUSED_DETAIL, create_operations_router

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
    Route("get", f"/sessions/{SID}/network-log", None, "get_network_log", permission=(403, OFF_LIST_PAGE_DETAIL)),
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
        # 503 for a full queue alone (test_agent_http.py): any other RuntimeError is a fault like the rest.
        value_error_is_generic=False,
    ),
    Route("post", "/agent/jobs/j-1/discard", None, "discard_job", owner="job_queue", value_error_is_generic=False),
    Route("post", "/agent/jobs/j-1/cancel", None, "cancel_job", owner="job_queue", value_error_is_generic=False),
    # A decision the approval's state refuses is a conflict, as an execute's is. The two had no typed_route_errors.
    Route("post", "/approvals/a-1/approve", {}, "approve", permission=(409, "Conflict")),
    Route("post", "/approvals/a-1/reject", {}, "reject", permission=(409, "Conflict")),
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
        logged = [r for r in caplog.records if r.name.startswith("app.routes") and r.levelno == logging.ERROR]
        assert logged, "the route answered 500 without logging the exception"
        # The error by its class and its text, quoted, in the record itself (internal_error): not by exc_info.
        assert ": answered 500 for RuntimeError: 'boom at /srv/secret/path'" in logged[0].getMessage()
        assert logged[0].exc_info is None


# The three errors a route answers with a fixed text. A server-side fault raises them too (a state file that no longer
# decodes is a ValueError, one the process may not read a PermissionError), and the answer says nothing of the cause.
FIXED_ANSWERS = {PermissionError: (403, "Not permitted"), ValueError: (400, "Invalid request")}
FIXED_ANSWERS[FileNotFoundError] = (404, "Not found")
LIST_SESSIONS = Route("get", "/sessions", None, "list_sessions")


def _warnings_of(caplog: pytest.LogCaptureFixture, logger: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == logger and r.levelno == logging.WARNING]


@pytest.mark.parametrize("error", FIXED_ANSWERS, ids=lambda error: error.__name__)
def test_an_error_answered_with_a_fixed_text_leaves_its_cause_in_the_log(
    client: TestClient, caplog: pytest.LogCaptureFixture, error: type[Exception]
) -> None:
    status, detail = FIXED_ANSWERS[error]
    with caplog.at_level(logging.WARNING, logger="app.routes.sessions"), _raising(LIST_SESSIONS, error("boom-cause")):
        response = _call(client, LIST_SESSIONS)

    assert (response.status_code, response.json()) == (status, {"detail": detail})
    (logged,) = _warnings_of(caplog, "app.routes.sessions")
    first, *where = logged.getMessage().splitlines()
    assert first == f"list sessions failed: answered {status} for {error.__name__}: 'boom-cause'"
    # Where it was raised: the frames of a traceback, the route's own among them.
    assert where and all(line.startswith("  ") for line in where), where
    assert any("sessions.py" in line for line in where), where


def test_a_state_file_that_no_longer_decodes_is_answered_as_before_and_logged(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    # A kill in the middle of an append can leave a multi-byte character cut in two. The route takes no input a
    # store could refuse, so "Invalid request" was all anyone was told, and the log held nothing.
    (tmp_path / "events.jsonl").write_bytes(b'{"half": "caf\xc3')
    store = FileAuditStore(tmp_path, max_events=1000)

    async def list_audit_events(**kwargs):
        return [event.model_dump() for event in await store.list(**kwargs)]

    application = FastAPI()
    manager = SimpleNamespace(list_audit_events=list_audit_events, sessions={})
    application.include_router(
        create_operations_router(manager=manager, proxy_store=SimpleNamespace(), cron_service=SimpleNamespace())
    )

    with caplog.at_level(logging.WARNING, logger="app.routes.operations"):
        response = TestClient(application, raise_server_exceptions=False).get("/audit/events")

    assert (response.status_code, response.json()) == (400, {"detail": "Invalid request"})
    (logged,) = _warnings_of(caplog, "app.routes.operations")
    first, *where = logged.getMessage().splitlines()
    assert first.startswith("audit events listing failed: answered 400 for UnicodeDecodeError: "), first
    assert "can't decode byte 0xc3" in first
    assert any("audit.py" in line for line in where), where


def test_a_line_break_in_a_routes_argument_or_in_the_errors_text_starts_no_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Both are the caller's as often as not: an id from the path, which an error then quotes.
    logger = logging.getLogger("app.routes.sessions")
    theirs = "a1\nWARNING:app.main:a line of the caller's own\r\x1b[2J"

    with caplog.at_level(logging.WARNING, logger=logger.name), pytest.raises(HTTPException) as answered:
        with typed_route_errors(logger, "get approval failed for approval %s", theirs):
            raise PermissionError(f"approval {theirs} is not approved")

    assert (answered.value.status_code, answered.value.detail) == (403, "Not permitted")
    (logged,) = _warnings_of(caplog, logger.name)
    message = logged.getMessage()
    first, *where = message.splitlines()
    assert "\r" not in message and "\x1b" not in message
    assert first.count("a line of the caller's own") == 2, first
    assert where and all(line.startswith("  ") for line in where), where


def test_an_error_that_keeps_its_own_answer_is_not_logged_as_a_hidden_cause(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("app.routes.sessions")
    kept = [
        (HTTPException(status_code=409, detail="busy"), HTTPException),
        (SessionNotFoundError("s1"), SessionNotFoundError),
        (BrowserUnreachableError("down"), HTTPException),
    ]

    for error, answered_as in kept:
        with caplog.at_level(logging.WARNING, logger=logger.name), pytest.raises(answered_as):
            with typed_route_errors(logger, "observe failed for session %s", "s1"):
                raise error

    assert _warnings_of(caplog, logger.name) == []


def _the_routes_line(caplog: pytest.LogCaptureFixture) -> tuple[str, list[str]]:
    """The one warning the routes wrote, as its first line and the frames under it."""
    (logged,) = [r for r in caplog.records if r.name.startswith("app.routes") and r.levelno == logging.WARNING]
    first, *where = logged.getMessage().splitlines()
    return first, where


@pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: r.id)
@pytest.mark.parametrize("error", FIXED_ANSWERS, ids=lambda error: error.__name__)
def test_every_route_leaves_the_cause_of_its_fixed_answer_in_the_log(
    client: TestClient, caplog: pytest.LogCaptureFixture, route: Route, error: type[Exception]
) -> None:
    # FB-1: a route that caught the class itself answered through an HTTPException, which typed_route_errors lets
    # pass, so its fixed text went out with nothing logged (open tab, a screenshot, an approval's "Conflict").
    if error is ValueError and not route.value_error_is_generic:
        pytest.skip("the route answers a ValueError with the error's own text")
    status, detail = route.permission if error is PermissionError else FIXED_ANSWERS[error]

    with caplog.at_level(logging.WARNING), _raising(route, error("boom-cause")):
        response = _call(client, route)

    assert (response.status_code, response.json()) == (status, {"detail": detail})
    first, where = _the_routes_line(caplog)
    assert first.endswith(f": answered {status} for {error.__name__}: 'boom-cause'"), first
    assert where and all(line.startswith("  ") for line in where), where


OWN_REFUSALS = [route for route in ALL_ROUTES if route.permission != (403, "Not permitted")]


@pytest.mark.parametrize("route", OWN_REFUSALS, ids=lambda r: r.id)
def test_a_permission_error_the_os_raised_is_not_answered_as_the_routes_own_refusal(
    client: TestClient, caplog: pytest.LogCaptureFixture, route: Route
) -> None:
    # FB-1: an approve or a reject while the approval directory refuses the write answered "Conflict", and a
    # screenshot or an observe the disk refuses said the page is on a host ALLOWED_HOSTS does not allow. One the
    # operating system raises carries an errno; none of the controller's own refusals does.
    denied = PermissionError(errno.EACCES, "Permission denied", "/data/approvals/a-1.json")

    with caplog.at_level(logging.WARNING), _raising(route, denied):
        response = _call(client, route)

    assert (response.status_code, response.json()) == (403, {"detail": "Not permitted"})
    first, _where = _the_routes_line(caplog)
    assert ": answered 403 for PermissionError: " in first and "Permission denied" in first, first


def _answered_and_logged(
    caplog: pytest.LogCaptureFixture, error: Exception, message: str = "thing failed", *args: object
) -> tuple[HTTPException, str]:
    """What typed_route_errors answers for ``error``, and the one line it writes."""
    logger = logging.getLogger("app.routes.sessions")
    with caplog.at_level(logging.WARNING, logger=logger.name), pytest.raises(HTTPException) as answered:
        with typed_route_errors(logger, message, *args):
            raise error
    (logged,) = _warnings_of(caplog, logger.name)
    return answered.value, logged.getMessage()


def test_a_long_error_text_is_cut_in_the_log_and_the_line_says_so(caplog: pytest.LogCaptureFixture) -> None:
    # The text quotes what the caller sent as often as not, and nothing bounded it (2 MB in the reader's probe).
    _answer, message = _answered_and_logged(caplog, ValueError("A" * 10_000))

    first = message.splitlines()[0]
    assert first.startswith("thing failed: answered 400 for ValueError: 'AAAA"), first[:80]
    assert len(first) < 400, len(first)
    assert "10000 characters" in first, first[-80:]


def test_a_long_argument_of_the_route_is_cut_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    # An id from the path, as long as the server lets a request line be.
    _answer, message = _answered_and_logged(caplog, ValueError("no"), "get thing failed for thing %s", "s" * 5000)

    first = message.splitlines()[0]
    assert first.startswith("get thing failed for thing 'ssss"), first[:80]
    assert len(first) < 400, len(first)


def _raise_from_depth(depth: int) -> None:
    if depth:
        _raise_from_depth(depth - 1)
        return
    raise ValueError("deep")


def test_only_the_frames_nearest_the_raise_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("app.routes.sessions")
    with caplog.at_level(logging.WARNING, logger=logger.name), pytest.raises(HTTPException):
        with typed_route_errors(logger, "thing failed"):
            _raise_from_depth(30)

    (logged,) = _warnings_of(caplog, logger.name)
    _first, *where = logged.getMessage().splitlines()
    # A line a frame, written by the controller: the file, the line and the function. What an interpreter prints of
    # a frame is its own (two lines on Python 3.12, and from 3.13 as many as the call is written over, which made
    # ten lines of a record thirteen), so the record was as long as the Python it ran on made it.
    code = _raise_from_depth.__code__
    calls, raises = code.co_firstlineno + 2, code.co_firstlineno + 4
    # The nearest three: two of the calls, and the raise itself last.
    assert where == [f"  {code.co_filename}:{line} in _raise_from_depth" for line in (calls, calls, raises)], where


class _PrintsALineBreak:
    def __str__(self) -> str:
        return "a\nINJECTED"

    __repr__ = __str__


class _OddStr(str):
    def __repr__(self) -> str:
        return "odd\nINJECTED BY REPR"


@pytest.mark.parametrize("argument", [_PrintsALineBreak(), _OddStr("odd")], ids=["an object", "a str subclass"])
def test_an_argument_that_prints_a_line_break_starts_no_log_line(
    caplog: pytest.LogCaptureFixture, argument: object
) -> None:
    # FB-10: only a str was written as its repr, by repr(), which a subclass answers as it likes.
    _answer, message = _answered_and_logged(caplog, ValueError("v"), "x failed for %s", argument)

    first, *where = message.splitlines()
    assert "INJECTED" in first, first
    assert where and all(line.startswith("  ") for line in where), where


class _CannotBePrinted:
    def __repr__(self) -> str:
        raise RuntimeError("no repr\nINJECTED")


def test_an_argument_that_cannot_be_printed_is_named_by_its_class_and_the_answer_stands(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # No route passes one today (ids from a path, and numbers). The line is written while an error is being
    # answered: a failure to print an argument must not take that answer's place.
    answer, message = _answered_and_logged(caplog, ValueError("v"), "x failed for %s", _CannotBePrinted())

    assert (answer.status_code, answer.detail) == (400, "Invalid request")
    first, *where = message.splitlines()
    assert first == "x failed for <a _CannotBePrinted that could not be printed>: answered 400 for ValueError: 'v'"
    assert where and all(line.startswith("  ") for line in where), where


def test_a_number_among_the_arguments_is_still_a_number(caplog: pytest.LogCaptureFixture) -> None:
    _answer, message = _answered_and_logged(caplog, ValueError("v"), "tab %d failed", 3)

    assert message.splitlines()[0] == "tab 3 failed: answered 400 for ValueError: 'v'"


class _NoText(ValueError):
    def __str__(self) -> str:
        raise RuntimeError("no text for you")


def test_an_error_whose_text_cannot_be_made_is_logged_by_its_class_and_still_answered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # FB-10: str(cause) ran outside any try, so such an error turned the fixed 400 into an unhandled exception.
    answer, message = _answered_and_logged(caplog, _NoText())

    assert (answer.status_code, answer.detail) == (400, "Invalid request")
    assert message.splitlines()[0].startswith("thing failed: answered 400 for _NoText: "), message


def test_a_models_refusal_is_logged_by_field_and_kind_and_never_by_the_value(caplog: pytest.LogCaptureFixture) -> None:
    # pydantic prints the value each refused field was given, and the cron trigger's body holds the webhook key.
    from app.tool_inputs import TriggerCronJobInput

    body = {"job_id": "j1", "webhook_key": ["sentinel-webhook-key"]}  # not on the line a frame quotes
    with pytest.raises(ValueError) as refused:
        TriggerCronJobInput.model_validate(body)
    assert "sentinel-webhook-key" in str(refused.value)  # what str() of it would have written

    _answer, message = _answered_and_logged(caplog, refused.value)

    assert "sentinel-webhook-key" not in message
    assert "webhook_key: string_type" in message.splitlines()[0], message


PERSONA_BODY = {"name": "us-east", "server": "http://proxy.example:8080"}


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (PermissionError("proxy persona 'us-east' belongs to another operator"), 409, "Conflict"),
        (PermissionError(errno.EACCES, "Permission denied", "/data/personas.json"), 403, "Not permitted"),
        (ValueError("persona server is required"), 400, "Invalid request"),
        (RuntimeError("No PROXY_PERSONA_FILE configured"), 400, "Invalid request"),
    ],
    ids=["another operator's", "the OS refuses the file", "a ValueError", "no file configured"],
)
def test_a_proxy_persona_that_is_not_saved_leaves_the_cause_in_the_log(
    client: TestClient, caplog: pytest.LogCaptureFixture, error: Exception, status: int, detail: str
) -> None:
    # Another operator's persona is a conflict. A file the operating system will not let the store write was
    # answered as one too, and none of the four left a line.
    with caplog.at_level(logging.WARNING), patch.object(main_module.proxy_store, "set_persona", side_effect=error):
        response = client.post("/proxy-personas", json=PERSONA_BODY)

    assert (response.status_code, response.json()) == (status, {"detail": detail})
    first, _where = _the_routes_line(caplog)
    assert first.startswith("set proxy persona failed for persona 'us-east': "), first
    assert f": answered {status} for {type(error).__name__}: " in first, first


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (PermissionError("auth profile 'shop' belongs to another operator"), 403, "Not permitted"),
        (ValueError("Invalid cron schedule 'every day': Wrong number of fields"), 400, "Invalid request"),
    ],
    ids=["PermissionError", "ValueError"],
)
def test_a_cron_job_that_is_not_created_leaves_the_cause_in_the_log(
    client: TestClient, caplog: pytest.LogCaptureFixture, error: Exception, status: int, detail: str
) -> None:
    with (
        caplog.at_level(logging.WARNING),
        patch.object(main_module.cron_service, "create_job", new=AsyncMock(side_effect=error)),
    ):
        response = client.post("/crons", json={"name": "nightly", "goal": "check the page", "schedule": "0 9 * * *"})

    assert (response.status_code, response.json()) == (status, {"detail": detail})
    first, _where = _the_routes_line(caplog)
    assert f": answered {status} for {type(error).__name__}: " in first and str(error.args[0]) in first, first


@pytest.mark.parametrize(
    ("verb", "path"),
    [("post", "/crons/{job_id}/webhook-key/rotate"), ("delete", "/crons/{job_id}/webhook-key")],
    ids=["rotate", "revoke"],
)
def test_a_cron_job_id_the_route_refuses_leaves_the_cause_in_the_log(
    client: TestClient, caplog: pytest.LogCaptureFixture, verb: str, path: str
) -> None:
    with caplog.at_level(logging.WARNING):
        response = getattr(client, verb)(path.format(job_id="j" * 51))

    assert (response.status_code, response.json()) == (400, {"detail": "Invalid request"})
    first, _where = _the_routes_line(caplog)
    assert ": answered 400 for ValidationError: " in first and "job_id: string_too_long" in first, first


def test_a_cron_trigger_refused_for_its_key_leaves_a_line_that_names_the_job(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # No job has this id: the answer is the one for a wrong key, and the log says a trigger was refused.
    with caplog.at_level(logging.WARNING):
        response = client.post("/crons/no-such-job/trigger", json={"webhook_key": "sentinel-key"})

    assert (response.status_code, response.json()) == (403, {"detail": "Not permitted"})
    first, _where = _the_routes_line(caplog)
    refused = "cron trigger refused for job 'no-such-job': answered 403 for WebhookKeyRefusedError: "
    assert first.startswith(refused), first
    assert "sentinel-key" not in first


def test_a_cron_trigger_with_a_body_the_route_refuses_logs_no_part_of_the_key(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # The body's key, sent in a shape the model refuses: pydantic's own text for it quotes the value.
    with caplog.at_level(logging.WARNING):
        response = client.post("/crons/job-1/trigger", json={"webhook_key": ["sentinel-webhook-key"]})

    assert (response.status_code, response.json()) == (400, {"detail": "Invalid request"})
    first, where = _the_routes_line(caplog)
    assert ": answered 400 for ValidationError: " in first and "webhook_key: string_type" in first, first
    assert "sentinel-webhook-key" not in "\n".join([first, *where])
    # A record of four lines at most, for a mistake any caller can repeat. The frames are pydantic's, a call written
    # over several lines: as Python 3.14 prints them the record was thirteen lines.
    assert 1 <= len(where) <= 3 and all(re.fullmatch(r"  .+:\d+ in \S+", line) for line in where), where


def test_a_cron_trigger_whose_body_the_json_reader_cannot_go_down_is_an_invalid_request(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # A body nested past what Python's JSON reader goes down raises RecursionError, a RuntimeError and no ValueError:
    # an unhandled 500, for a caller who needs no key of the job. How deep that is is the interpreter's own, so the
    # reader is stood in for.
    gave_up = RecursionError("maximum recursion depth exceeded while decoding a JSON object from a unicode string")
    with caplog.at_level(logging.WARNING), patch.object(Request, "json", new=AsyncMock(side_effect=gave_up)):
        response = client.post("/crons/job-1/trigger", json={"webhook_key": "sentinel-key"})

    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "Invalid request"}
    first, _where = _the_routes_line(caplog)
    assert first.startswith("cron trigger failed for job 'job-1': answered 400 for RecursionError: "), first


def test_a_cron_trigger_whose_run_raises_a_value_error_leaves_the_cause_in_the_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # Raised with the key proved, so it is no "Invalid request" of the caller's: the 409 of a run that cannot start.
    error = ValueError("start_url must use http or https")
    with (
        caplog.at_level(logging.WARNING),
        patch.object(main_module.cron_service, "trigger_via_webhook", new=AsyncMock(side_effect=error)),
    ):
        response = client.post("/crons/job-1/trigger", json={"webhook_key": "sentinel-key"})

    assert (response.status_code, response.json()) == (409, {"detail": CRON_TRIGGER_RUN_REFUSED_DETAIL})
    first, _where = _the_routes_line(caplog)
    assert first == f"cron trigger failed for job 'job-1': answered 409 for ValueError: {str(error)!r}"


def test_a_share_refused_for_its_lifetime_leaves_the_cause_in_the_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.WARNING),
        patch.object(main_module.manager, "get_session", new=AsyncMock()),
        patch.object(main_module.share_manager, "create_token", side_effect=ValueError("ttl_seconds must be positive")),
    ):
        response = client.post(f"/sessions/{SID}/share")

    assert (response.status_code, response.json()) == (400, {"detail": "Invalid request"})
    first, _where = _the_routes_line(caplog)
    assert first == f"share failed for session '{SID}': answered 400 for ValueError: 'ttl_seconds must be positive'"


def _shared_observe(client: TestClient, error: Exception):
    token_info = {"valid": True, "session_id": SID}
    with (
        patch.object(main_module.share_manager, "token_info", return_value=token_info),
        patch.object(main_module.manager, "share_snapshot", new=AsyncMock(side_effect=error)),
    ):
        return client.get("/share/sentinel-token/observe")


def test_a_shared_observe_the_os_refuses_is_a_logged_server_error_and_no_refused_page(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # It told the link's viewer that the page is on a host ALLOWED_HOSTS does not allow, and logged nothing.
    denied = PermissionError(errno.EACCES, "Permission denied", "/data/artifacts/s1/frame.png")
    with caplog.at_level(logging.WARNING):
        response = _shared_observe(client, denied)

    assert (response.status_code, response.json()) == (500, {"detail": "Internal error"})
    logged = [r for r in caplog.records if r.name == "app.routes.share" and r.levelno == logging.ERROR]
    assert logged and ": answered 500 for PermissionError: " in logged[0].getMessage()
    assert "[Errno 13] Permission denied" in logged[0].getMessage()


def test_a_shared_observe_of_a_refused_page_is_answered_as_before_and_writes_no_line(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # The shared page asks every five seconds for as long as it is open: its refusal is not a line each time.
    with caplog.at_level(logging.WARNING):
        response = _shared_observe(client, PermissionError("Host 'evil.example' is not allowlisted"))

    assert (response.status_code, response.json()) == (403, {"detail": OFF_LIST_PAGE_DETAIL})
    assert [r for r in caplog.records if r.name.startswith("app.routes")] == []


APPROVAL_ID_ROUTES = [
    Route("get", "/approvals/{}", None, "get_approval"),
    Route("get", "/approvals/{}/arguments", None, "approval_arguments"),
    Route("post", "/approvals/{}/approve", {}, "approve"),
    Route("post", "/approvals/{}/reject", {}, "reject"),
    Route("post", "/approvals/{}/execute", None, "execute_approval"),
]


def _with_id(route: Route, record_id: str) -> Route:
    return replace(route, path=route.path.format(record_id))


@pytest.mark.parametrize("route", APPROVAL_ID_ROUTES, ids=lambda r: r.id)
def test_an_unknown_approval_id_is_an_ordinary_404_and_writes_no_line(
    client: TestClient, caplog: pytest.LogCaptureFixture, route: Route
) -> None:
    # The store raises a plain KeyError(approval_id) and nothing mapped it, so the app's handler answered it: with
    # its warning for a KeyError nothing maps, forty lines of traceback at WARNING for a caller's typo.
    with caplog.at_level(logging.WARNING):
        response = _call(client, _with_id(route, "nope"))

    assert (response.status_code, response.json()) == (404, {"detail": "Not found: nope"})
    assert [r.getMessage() for r in caplog.records if r.name.startswith("app")] == []


@pytest.mark.parametrize("route", APPROVAL_ID_ROUTES, ids=lambda r: r.id)
def test_an_unknown_approval_id_of_any_length_is_cut_in_the_answer(client: TestClient, route: Route) -> None:
    # The 404 repeated the id whole: a path of 60,000 characters was answered with a body of 60,025.
    response = _call(client, _with_id(route, "x" + "y" * 60_000))

    assert response.status_code == 404, response.text[:200]
    detail = response.json()["detail"]
    assert detail.startswith("Not found: xyyyy") and detail.endswith("y..."), detail[-40:]
    assert len(response.text) < 300


@pytest.mark.parametrize("route", APPROVAL_ID_ROUTES, ids=lambda r: r.id)
def test_an_approval_routes_other_key_errors_are_not_an_unknown_approval(
    client: TestClient, caplog: pytest.LogCaptureFixture, route: Route
) -> None:
    # Only the KeyError that names the id asked for is the store's "no such approval". Any other still reaches the
    # app's handler: a bug's is logged there, and a session that has gone keeps its own answer.
    route = _with_id(route, "a-1")
    with caplog.at_level(logging.WARNING, logger="app.main"), _raising(route, KeyError("internal_dict_key")):
        response = _call(client, route)
    assert (response.status_code, response.json()) == (404, {"detail": "Not found: internal_dict_key"})
    assert ["unmapped KeyError" in r.getMessage() for r in caplog.records if r.name == "app.main"] == [True]

    with _raising(route, SessionNotFoundError("a-1", status="closed")):
        response = _call(client, route)
    assert (response.status_code, response.json()["code"]) == (404, "session_closed")


# Three more routes whose service raises a plain KeyError for an id it does not hold, and what their 404 says.
UNKNOWN_ID_ROUTES = [
    (Route("get", "/auth-profiles/{}", None, "get_auth_profile"), "Not found: {}"),
    (Route("get", "/crons/{}", None, "get_job", owner="cron_service"), "Not found: Cron job not found: {}"),
    (Route("get", "/sessions/{}/auth-state", None, "get_auth_state_info"), "Not found: {}"),
]
_UNKNOWN_ID_ROUTE_IDS = [route.id for route, _ in UNKNOWN_ID_ROUTES]


def _app_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name.startswith("app") and r.levelno >= logging.WARNING]


@pytest.mark.parametrize(("route", "detail"), UNKNOWN_ID_ROUTES, ids=_UNKNOWN_ID_ROUTE_IDS)
def test_an_unknown_id_is_an_ordinary_404_and_writes_no_line(
    client: TestClient, caplog: pytest.LogCaptureFixture, route: Route, detail: str
) -> None:
    # No patching: the real service is asked for an id it does not hold. Each raised a plain KeyError that nothing
    # mapped, so the app's handler answered it and wrote its WARNING, with where it was raised, for a caller's typo.
    with caplog.at_level(logging.WARNING):
        response = _call(client, _with_id(route, "nope-1"))

    assert (response.status_code, response.json()) == (404, {"detail": detail.format("nope-1")})
    assert _app_warnings(caplog) == []


def test_an_unknown_auth_profile_named_with_spaces_round_it_is_that_404_too(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # The service looks a name up stripped, and its KeyError carries the name so.
    with caplog.at_level(logging.WARNING):
        response = client.get("/auth-profiles/%20nope-1%20")

    assert (response.status_code, response.json()) == (404, {"detail": "Not found: nope-1"})
    assert _app_warnings(caplog) == []


@pytest.mark.parametrize(("route", "detail"), UNKNOWN_ID_ROUTES[1:], ids=_UNKNOWN_ID_ROUTE_IDS[1:])
def test_an_unknown_id_of_any_length_is_cut_in_its_404_and_writes_no_line(
    client: TestClient, caplog: pytest.LogCaptureFixture, route: Route, detail: str
) -> None:
    # A profile's name is 120 characters at most, and a longer one is a 400. These two ids are as long as the path:
    # the session store raises the same KeyError for an id that could name no record.
    long_id = "x" + "y" * 3000
    with caplog.at_level(logging.WARNING):
        response = _call(client, _with_id(route, long_id))

    key = detail.format(long_id).removeprefix("Not found: ")
    assert (response.status_code, response.json()) == (404, {"detail": f"Not found: {key[:200]}..."})
    assert _app_warnings(caplog) == []


# Routes that answer an id they do not know with a 404 of their own words, which name the id. No patching: the real
# service, or the real manager, holds no such persona, job or session.
NO_INSPECTOR, NO_CDP = "No network inspector for session {!r}", "No CDP session for {!r}"
OWN_WORDS_404_ROUTES = [
    (
        Route("delete", "/proxy-personas/{}", None, "delete_persona", owner="proxy_store", sync=True),
        "Proxy persona not found: {!r}",
    ),
    (Route("delete", "/crons/{}", None, "delete_job", owner="cron_service"), "Cron job not found: {}"),
    (Route("get", "/sessions/{}/network/requests", None, "sessions.get", sync=True), NO_INSPECTOR),
    (Route("get", "/sessions/{}/network/hooks", None, "sessions.get", sync=True), NO_INSPECTOR),
    (Route("post", "/sessions/{}/network/hooks", {"url_pattern": "*"}, "sessions.get", sync=True), NO_INSPECTOR),
    (Route("delete", "/sessions/{}/network/hooks/x", None, "sessions.get", sync=True), NO_INSPECTOR),
    (Route("get", "/sessions/{}/cdp/element?selector=a", None, "sessions.get", sync=True), NO_CDP),
    (Route("post", "/sessions/{}/cdp/raw", {"method": "DOM.getDocument"}, "sessions.get", sync=True), NO_CDP),
]


@pytest.mark.parametrize(("route", "words"), OWN_WORDS_404_ROUTES, ids=[route.id for route, _ in OWN_WORDS_404_ROUTES])
def test_a_404_in_a_routes_own_words_cuts_a_long_id(client: TestClient, route: Route, words: str) -> None:
    # Each repeated the caller's id whole: an id of 60,000 characters came back in a body of 60,033 bytes or more.
    long_id = "x" + "y" * 59_999
    response = _call(client, _with_id(route, long_id))

    assert (response.status_code, response.json()) == (404, {"detail": words.format(long_id[:200] + "...")})
    assert len(response.content) < 1024
    # An id of an ordinary length is named whole, in the route's words as before.
    response = _call(client, _with_id(route, "nope-1"))
    assert (response.status_code, response.json()) == (404, {"detail": words.format("nope-1")})


@pytest.mark.parametrize("route", [route for route, _ in UNKNOWN_ID_ROUTES], ids=_UNKNOWN_ID_ROUTE_IDS)
def test_these_routes_other_key_errors_are_not_an_unknown_id(
    client: TestClient, caplog: pytest.LogCaptureFixture, route: Route
) -> None:
    # Only the KeyError the service raises for the id asked for is its "no such record". Any other still reaches the
    # app's handler: a bug's is logged there, and a session that has gone keeps its own answer.
    route = _with_id(route, "a-1")
    with caplog.at_level(logging.WARNING, logger="app.main"), _raising(route, KeyError("internal_dict_key")):
        response = _call(client, route)
    assert (response.status_code, response.json()) == (404, {"detail": "Not found: internal_dict_key"})
    assert ["unmapped KeyError" in r.getMessage() for r in caplog.records if r.name == "app.main"] == [True]

    with _raising(route, SessionNotFoundError("a-1", status="closed")):
        response = _call(client, route)
    assert (response.status_code, response.json()["code"]) == (404, "session_closed")


def _unmapped(client: TestClient, caplog: pytest.LogCaptureFixture, error: KeyError, route: Route = SESSION_ROUTES[0]):
    """The answer to a KeyError nothing maps, and the one record the app's handler writes for it."""
    with caplog.at_level(logging.WARNING, logger="app.main"), _raising(route, error):
        response = _call(client, route)
    assert response.status_code == 404, response.text[:200]
    logged = [r for r in caplog.records if r.name == "app.main" and r.levelno == logging.WARNING]
    assert logged, "an unmapped KeyError was answered 404 without a log line"
    (record,) = logged
    assert "unmapped KeyError" in record.getMessage()
    return response, record


def _as_a_handler_writes(record: logging.LogRecord) -> str:
    """The record's message and whatever a handler writes under it: the traceback of exc_info, a stack."""
    return logging.Formatter().format(record)


def test_unmapped_key_error_is_404_and_logged(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    # A plain KeyError from a bug looks the same to the client as an unknown id,
    # so the handler is the only place it can be told apart.
    response, record = _unmapped(client, caplog, KeyError("internal_dict_key"))
    assert response.json() == {"detail": "Not found: internal_dict_key"}
    first, *where = record.getMessage().splitlines()
    assert first.startswith(f"unmapped KeyError on 'GET' '/sessions/{SID}/observe': answered 404 for KeyError: ")
    assert "internal_dict_key" in first
    # Where it was raised is in the message (log_hidden_cause), a line a frame and three at most, and not a
    # traceback a handler prints under it.
    assert 1 <= len(where) <= 3 and all(re.fullmatch(r"  .+:\d+ in \S+", line) for line in where), where
    assert record.exc_info is None and _as_a_handler_writes(record) == record.getMessage()


def test_an_unmapped_key_errors_log_line_shows_a_control_character_of_the_path_escaped(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # request.url.path drops a line feed and a carriage return and nothing else: an ESC or a vertical tab the caller
    # put in the path reached the log as itself.
    route = Route("get", "/sessions/no%1B[2Jpe%0B", None, "get_session_record")
    _response, record = _unmapped(client, caplog, KeyError("internal_dict_key"), route)
    message = record.getMessage()
    assert "\x1b" not in message and "\x0b" not in message
    assert "\\x1b[2Jpe" in message


def test_an_unmapped_key_errors_log_line_cuts_a_path_of_any_length(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # The path is the caller's, as long as the server lets a request line be.
    route = Route("get", "/sessions/" + "s" * 5000, None, "get_session_record")
    _response, record = _unmapped(client, caplog, KeyError("internal_dict_key"), route)
    first = record.getMessage().splitlines()[0]
    assert "/sessions/ssss" in first
    assert len(first) < 400


def test_an_unmapped_key_errors_whole_record_is_cut_whatever_the_key_holds(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # A KeyError quotes its key, an id as often as not, and the record carried the error whole (exc_info) under a
    # path cut at 200 characters: an id of 60,000 wrote a record of 63,000, and the 404 repeated it as well.
    response, record = _unmapped(client, caplog, KeyError("k" * 60_000))

    first = record.getMessage().splitlines()[0]
    assert "kkkk" in first and "(cut from 60" in first and first.endswith(" characters)"), first[-80:]
    assert len(first) < 600
    assert "k" * 300 not in _as_a_handler_writes(record)
    detail = response.json()["detail"]
    assert detail.startswith("Not found: kkkk") and detail.endswith("k..."), detail[-40:]
    assert len(response.text) < 300


class _TextAsItIs(KeyError):
    """A KeyError that prints its text unquoted, as SessionNotFoundError does: a plain one prints its key's repr."""

    def __str__(self) -> str:
        return str(self.args[0])


def test_an_unmapped_key_errors_text_starts_no_line_of_the_record(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    _response, record = _unmapped(client, caplog, _TextAsItIs("id\nWARNING:app.main:forged by a caller\r\x85end"))

    first, *where = _as_a_handler_writes(record).splitlines()
    assert "forged by a caller" in first
    assert [line for line in where if "forged" in line] == [], where


# A session id as a caller can write one in a path: a line break, a carriage return and an ESC.
FORGED_ID = "x%0AWARNING:app.main:forged by a caller%0D%1B[2J"
# Two lines beside the routes' fixed answers, which quote and cut what the caller wrote. Shadow browsing is off by
# default and is refused before the session is looked up, so its line held the id as it came; no record answers to
# the id the second route is asked for, and the KeyError the app's handler logs quotes it.
LINES_THAT_NAME_THE_SESSION = [
    ("post", "/sessions/{id}/shadow-browse", 400, "app.routes.session_diagnostics", "shadow browse refused for "),
    ("get", "/sessions/{id}/witness/verify", 404, "app.main", "unmapped KeyError on 'GET' "),
]


@pytest.mark.parametrize("more", ["", "A" * 3000], ids=["a short id", "a long one"])
@pytest.mark.parametrize(
    ("verb", "path", "status", "logger", "begins"), LINES_THAT_NAME_THE_SESSION, ids=["shadow browse", "unmapped"]
)
def test_a_session_id_with_a_line_break_starts_no_line_of_the_log(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    verb: str,
    path: str,
    status: int,
    logger: str,
    begins: str,
    more: str,
) -> None:
    with caplog.at_level(logging.WARNING):
        response = getattr(client, verb)(path.replace("{id}", FORGED_ID + more))

    assert response.status_code == status, response.text[:200]
    (record,) = _warnings_of(caplog, logger)
    written = _as_a_handler_writes(record)
    first, *where = written.splitlines()
    assert first.startswith(begins) and "forged by a caller" in first, first[:200]
    assert [line for line in where if "forged" in line] == [], where
    assert "\r" not in written and "\x1b" not in written
    assert len(written) < 1000, len(written)


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
