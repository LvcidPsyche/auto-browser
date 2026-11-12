from __future__ import annotations

import contextlib
import hmac
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Annotated, Any, NamedTuple

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..action_errors import RefusedPageError
from ..api_token import TAKEOVER_LINK_UNAVAILABLE_DETAIL
from ..audit import get_current_operator
from ..auth_state import AuthStateDecryptError
from ..browser.services.session_creation import SessionLimitError
from ..browser.services.tabs import TabRefusedByAnotherTabError, TabUnjudgedError
from ..models import (
    HTTP_URL_SCHEMES,
    ClickRequest,
    CreateSessionRequest,
    ExecuteActionRequest,
    HoverRequest,
    HumanTakeoverRequest,
    NavigateRequest,
    ObserveRequest,
    OpenTabRequest,
    OperatorIdentity,
    PressRequest,
    ProtectionMode,
    ScreenshotRequest,
    ScrollRequest,
    SelectOptionRequest,
    TabIndexRequest,
    TypeRequest,
    UploadRequest,
    WaitRequest,
    validate_url,
)
from ..session_ownership import acting_as, verified_operator
from ..url_safety import refuse_another_navigation_host
from ..utils import error_text
from ._utils import (
    OFF_LIST_OTHER_TAB_DETAIL,
    OFF_LIST_PAGE_DETAIL,
    REFUSED_PAGE,
    REFUSED_TAB,
    TAKEOVER_LINK_ROUTE,
    UNJUDGED_TAB_DETAIL,
    log_hidden_cause,
    persona_file_errors,
    require_safe_segment,
    typed_route_errors,
    unknown_id_is_404,
)

logger = logging.getLogger(__name__)

# One text for a missing and a wrong takeover token, and for a session that exists and one that does not. It names
# only the header, not where the token lives: every caller without the token reads it (W2-22).
TAKEOVER_LINK_REFUSED_DETAIL = "This endpoint needs the takeover token in the X-Takeover-Token header."

# How long later takeover-link refusals from one source are only counted, and how many sources are remembered at once.
TAKEOVER_REFUSAL_WINDOW_SECONDS = 60.0
TAKEOVER_REFUSAL_SOURCES_MAX = 1024


@dataclass(frozen=True, slots=True)
class RefusedCall:
    """One refused takeover-link call, as its audit event describes it."""

    session_id: str
    reason: str
    operator: OperatorIdentity


class HeldBack(NamedTuple):
    """What an event reports beside its own refusal: the refusals that were only counted."""

    count: int
    # Some of them named a session other than the event's: its count is not all against that session.
    other_sessions: bool


# An event to write for what a source's window holds: the source, the call the event describes, and its count.
HeldRefusals = tuple[str, RefusedCall, HeldBack]


@dataclass(slots=True)
class _RefusalWindow:
    started_at: float
    counted: int = 0
    # The last refusal that was only counted: the event written for what is held is its event (take_held).
    last: RefusedCall | None = None
    # The session the counted refusals named, while they have all named one.
    session_id: str | None = None
    several_sessions: bool = False

    def count(self, call: RefusedCall) -> None:
        self.counted += 1
        self.last = call
        if self.session_id is None:
            self.session_id = call.session_id
        elif self.session_id != call.session_id:
            self.several_sessions = True

    def held_beside(self, call: RefusedCall) -> HeldBack:
        """What an event for ``call`` reports of the counted refusals."""
        return HeldBack(self.counted, self.several_sessions or self.session_id not in (None, call.session_id))

    def take(self) -> tuple[RefusedCall, HeldBack] | None:
        """The counted refusals as one event, the last of them with the others as its count; nothing stays held."""
        if self.last is None:
            return None
        taken = self.last, HeldBack(self.counted - 1, self.several_sessions)
        self.counted, self.last, self.session_id, self.several_sessions = 0, None, None, False
        return taken


class TakeoverRefusalWindows:
    """Turns a flood of refused takeover-link calls into few audit events, whose counts add up to the refusals.

    The first refusal from a source audits as it always did. Later ones in the same window are only counted, and
    one event reports them: the source's next refusal after the window, or, if none has come, the event of the last
    of them, written when what is held is taken (take_held: by the maintenance sweep once the window is over, at the
    source's next read with the right token, and at shutdown). Either way every refusal is one event or one of an
    event's count, so the events and their counts add up to the refusals. How many events a window sees is not
    fixed: a read with the right token writes what is held and the window stands, so refusals between such reads
    write an event after each. A source is the verified operator, else the client host; the map is bounded, and a
    source pushed out of it is forgotten with what it held, and audits as new. A refusal answers 403 every time.
    """

    def __init__(self) -> None:
        self._windows: OrderedDict[str, _RefusalWindow] = OrderedDict()

    def record(self, source: str, call: RefusedCall, *, now: float) -> HeldBack | None:
        """What the event due now reports beside ``call``, or None when ``call`` is only counted."""
        window = self._windows.get(source)
        if window is not None and now - window.started_at < TAKEOVER_REFUSAL_WINDOW_SECONDS:
            window.count(call)
            return None
        self._windows[source] = _RefusalWindow(started_at=now)
        self._windows.move_to_end(source)
        while len(self._windows) > TAKEOVER_REFUSAL_SOURCES_MAX:
            self._windows.popitem(last=False)
        return window.held_beside(call) if window is not None else HeldBack(0, False)

    def _chosen(self, now: float | None, source: str | None) -> list[tuple[str, _RefusalWindow]]:
        """The windows a taking is of: those over at ``now`` (all of them with no time),
        of ``source`` alone if named.
        """
        named = self._windows.items() if source is None else [(source, self._windows.get(source))]
        return [
            (name, window)
            for name, window in named
            if window is not None and (now is None or now - window.started_at >= TAKEOVER_REFUSAL_WINDOW_SECONDS)
        ]

    def sources_holding(self, *, now: float | None = None) -> list[str]:
        """The sources take_held would take something from, in its order; nothing is taken."""
        return [name for name, window in self._chosen(now, None) if window.last is not None]

    def take_held(self, *, now: float | None = None, source: str | None = None) -> list[HeldRefusals]:
        """Take what is held, as the event to write for each source: (source, the call it describes, its count).

        Of every window that is over at ``now``, and of every window, over or not, when no time is given; of
        ``source``'s alone when one is named. The windows stand: a refusal in one still running is counted again.
        """
        return [(name, *taken) for name, window in self._chosen(now, source) if (taken := window.take()) is not None]

    def event_not_written(self, source: str, call: RefusedCall, held: HeldBack) -> None:
        """The event for ``call``, which was to report ``held``, was not written: both are held again.

        The window no longer stands, so the source's next refusal writes an event instead of being counted, and so
        does the next taking of what is held. A refusal counted while the write failed stays the last one. A source
        pushed out of the map meanwhile audits as new, as any other.
        """
        window = self._windows.get(source)
        if window is None:
            return
        counted_meanwhile = window.last
        window.started_at = -math.inf
        window.count(call)
        window.counted += held.count
        window.several_sessions = window.several_sessions or held.other_sessions
        if counted_meanwhile is not None:
            window.last = counted_meanwhile


async def _write_refusal_event(
    manager: Any, windows: TakeoverRefusalWindows, source: str, call: RefusedCall, held: HeldBack
) -> None:
    """The audit event of ``call`` and of what it reports beside itself, as the operator who was refused."""
    details: dict[str, Any] = {"reason": call.reason}
    if held.count:
        details["count"] = held.count
    if held.other_sessions:
        details["counted_other_sessions"] = True
    try:
        with acting_as(call.operator):
            await manager.audit.append(
                event_type="takeover_link_refused",
                status="refused",
                action="get_takeover_link",
                session_id=call.session_id,
                details=details,
            )
    except BaseException:
        # Cancelled too: the window opens only once its event is written. A store that wrote it and then failed, or a
        # cancelled append that lands behind this (the store writes in a worker thread), has the event's refusals
        # reported twice, its own and every one of its count, where the other way would lose them.
        windows.event_not_written(source, call, held)
        raise


async def write_held_takeover_refusals(
    manager: Any, windows: TakeoverRefusalWindows, *, finished_only: bool = False
) -> None:
    """Write the event of the refusals each source's window holds: every window's, or only those that are over.

    For shutdown (every window) and the maintenance sweep (a window that is over): a burst that stops would
    otherwise leave its first refusal as the only one in the audit log. No request waits on these writes, so one
    that fails is logged, what it was to report stays held, and the rest are still tried.

    Each source's is taken just before its own write. Taken from every window first, a sweep cancelled in one write
    (maintenance.shutdown() cancels the sweep under way) had emptied the windows it never came to, and the
    shutdown's drain found nothing in them to write. A source that opened a new window meanwhile is left to it.
    """
    now = time.monotonic() if finished_only else None
    for source in windows.sources_holding(now=now):
        for held_source, call, held in windows.take_held(now=now, source=source):
            try:
                await _write_refusal_event(manager, windows, held_source, call, held)
            except Exception:
                logger.warning(
                    "%d refused takeover-link calls that were only counted could not be audited: still held",
                    held.count + 1,
                    exc_info=True,
                )


def _refusal_source(request: Request) -> str:
    """Who keeps being refused: the verified operator id, else the client host (a header claims nothing here)."""
    operator = verified_operator()
    return f"operator:{operator}" if operator else f"host:{request.client.host if request.client else 'unknown'}"


def create_sessions_router(*, manager: Any, refusal_windows: TakeoverRefusalWindows | None = None) -> APIRouter:
    """``refusal_windows``: the takeover-link refusals that are only counted, for whoever else writes what they hold
    (write_held_takeover_refusals); the router's own when none is given."""
    router = APIRouter()
    if refusal_windows is None:
        refusal_windows = TakeoverRefusalWindows()

    @router.get("/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        with typed_route_errors(logger, "list sessions failed"):
            return await manager.list_sessions()

    @router.post("/sessions")
    async def create_session(payload: CreateSessionRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "create session failed"):
            try:
                with persona_file_errors():
                    created = await manager.create_session(
                        name=payload.name,
                        start_url=payload.start_url,
                        storage_state_path=payload.storage_state_path,
                        auth_profile=payload.auth_profile,
                        memory_profile=payload.memory_profile,
                        proxy_persona=payload.proxy_persona,
                        request_proxy_server=payload.proxy_server,
                        request_proxy_username=payload.proxy_username,
                        request_proxy_password=payload.proxy_password,
                        user_agent=payload.user_agent,
                        protection_mode=payload.protection_mode,
                        totp_secret=payload.totp_secret,
                        totp_hosts=payload.totp_hosts,
                    )
            except SessionLimitError as exc:
                raise HTTPException(status_code=409, detail=error_text(exc)) from None
            except AuthStateDecryptError as exc:
                # A fixed text that says what to check; it names no path.
                raise HTTPException(status_code=422, detail=error_text(exc)) from None
            except RuntimeError as exc:
                # The caller gets the generic answer; the cause is here, and nowhere else: as a hidden cause, since a
                # traceback prints the error's text as it is (a persona's name or a URL the caller sent).
                log_hidden_cause(logger, 409, exc, "create session failed")
                raise HTTPException(status_code=409, detail="Conflict") from None
            return created

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "get session failed for session %s", session_id):
            return await manager.get_session_record(session_id)

    @router.get(TAKEOVER_LINK_ROUTE)
    async def get_takeover_link(
        session_id: str,
        request: Request,
        x_takeover_token: Annotated[str | None, Header(alias="X-Takeover-Token")] = None,
    ) -> JSONResponse:
        # Human-only (NAV-14): the full link, an isolated session's noVNC password in its fragment. No MCP tool and
        # no agent-facing package calls this; the dashboard and scripts/takeover_link.sh do. Beside the bearer
        # token, which agents hold, it demands the takeover token (NAV-14b), in every scope, tokenless loopback
        # included, and before the session lookup, so a caller without it cannot tell which sessions exist: the
        # route is exempt from the app-wide require_session_access (W2-19). A malformed id is a 400 first; a
        # closed, unknown or other operator's session is then a 404 (get_session). The audit events name the read
        # or the refusal, never a token, the URL or the password.
        require_safe_segment(session_id, field="session_id")
        expected = getattr(manager.settings, "takeover_token", None)
        if not expected:
            raise HTTPException(status_code=503, detail=TAKEOVER_LINK_UNAVAILABLE_DETAIL)
        # Spaces and tabs around the token are not part of it, as normalize_authorization treats the bearer token.
        presented = (x_takeover_token or "").strip(" \t").encode("utf-8")
        source = _refusal_source(request)
        if not hmac.compare_digest(presented, expected.encode("utf-8")):
            reason = "missing takeover token" if not x_takeover_token else "wrong takeover token"
            call = RefusedCall(session_id=session_id, reason=reason, operator=get_current_operator())
            held = refusal_windows.record(source, call, now=time.monotonic())
            if held is not None:
                await _write_refusal_event(manager, refusal_windows, source, call, held)
            raise HTTPException(status_code=403, detail=TAKEOVER_LINK_REFUSED_DETAIL)
        # The right token ends a burst from this source: what was only counted is written now, before the read's
        # own event, and not left for a refusal that may never come.
        for held_source, call, held in refusal_windows.take_held(source=source):
            await _write_refusal_event(manager, refusal_windows, held_source, call, held)
        session = await manager.get_session(session_id)
        link = manager.remote_access.takeover_link(session)
        await manager.audit.append(
            event_type="takeover_link_viewed",
            status="ok",
            action="get_takeover_link",
            session_id=session.id,
            details={},
        )
        return JSONResponse(link, headers={"Cache-Control": "no-store"})

    @router.get("/sessions/{session_id}/observe")
    async def observe(session_id: str, limit: int = 40, preset: str | None = None) -> dict[str, Any]:
        with typed_route_errors(logger, "observe failed for session %s", session_id, refusal=REFUSED_PAGE):
            return await manager.observe(session_id, limit=limit, preset=preset)

    @router.post("/sessions/{session_id}/observe")
    async def observe_post(session_id: str, payload: ObserveRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "observe failed for session %s", session_id, refusal=REFUSED_PAGE):
            return await manager.observe(session_id, limit=payload.limit, preset=payload.preset)

    @router.post("/sessions/{session_id}/screenshot")
    async def capture_screenshot(session_id: str, payload: ScreenshotRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "screenshot failed for session %s", session_id, refusal=REFUSED_PAGE):
            return await manager.capture_screenshot(session_id, label=payload.label)

    @router.get("/sessions/{session_id}/downloads")
    async def list_downloads(session_id: str) -> list[dict[str, Any]]:
        with typed_route_errors(logger, "list downloads failed for session %s", session_id):
            return await manager.list_downloads(session_id)

    @router.get("/sessions/{session_id}/tabs")
    async def list_tabs(session_id: str) -> list[dict[str, Any]]:
        with typed_route_errors(logger, "list tabs failed for session %s", session_id):
            return await manager.list_tabs(session_id)

    @router.post("/sessions/{session_id}/tabs/activate")
    async def activate_tab(session_id: str, payload: TabIndexRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "activate tab failed for session %s", session_id, refusal=REFUSED_TAB):
            try:
                return await manager.activate_tab(session_id, payload.index)
            except TabRefusedByAnotherTabError:
                raise HTTPException(status_code=403, detail=OFF_LIST_OTHER_TAB_DETAIL) from None
            except TabUnjudgedError:
                raise HTTPException(status_code=403, detail=UNJUDGED_TAB_DETAIL) from None

    @router.post("/sessions/{session_id}/tabs/close")
    async def close_tab(session_id: str, payload: TabIndexRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "close tab failed for session %s", session_id):
            return await manager.close_tab(session_id, payload.index)

    @router.post("/sessions/{session_id}/tabs/open")
    async def open_tab(session_id: str, payload: OpenTabRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "open tab failed for session %s", session_id):
            return await manager.open_tab(session_id, payload.url, payload.activate)

    @router.post("/sessions/{session_id}/actions/navigate")
    async def navigate(session_id: str, payload: NavigateRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "navigate failed for session %s", session_id):
            return await manager.navigate(session_id, payload.url)

    @router.post("/sessions/{session_id}/actions/click")
    async def click(session_id: str, payload: ClickRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "click failed for session %s", session_id):
            return await manager.click(
                session_id,
                selector=payload.selector,
                element_id=payload.element_id,
                x=payload.x,
                y=payload.y,
            )

    @router.post("/sessions/{session_id}/actions/type")
    async def type_text(session_id: str, payload: TypeRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "type failed for session %s", session_id):
            return await manager.type(
                session_id,
                selector=payload.selector,
                element_id=payload.element_id,
                text=payload.text,
                clear_first=payload.clear_first,
                sensitive=payload.sensitive,
            )

    @router.post("/sessions/{session_id}/actions/press")
    async def press_key(session_id: str, payload: PressRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "press failed for session %s", session_id):
            return await manager.press(session_id, payload.key)

    @router.post("/sessions/{session_id}/actions/scroll")
    async def scroll(session_id: str, payload: ScrollRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "scroll failed for session %s", session_id):
            return await manager.scroll(session_id, payload.delta_x, payload.delta_y)

    # The two actions that take an approval's id answer one no record answers to (and another operator's, which
    # the store answers so) with their own 404, as the approval routes do: through the app's handler for a KeyError
    # nothing maps, each wrote a WARNING. An action that takes none has no such id to answer for.
    def approval_id_errors(approval_id: str | None) -> contextlib.AbstractContextManager[None]:
        return unknown_id_is_404(approval_id) if approval_id else contextlib.nullcontext()

    @router.post("/sessions/{session_id}/actions/execute")
    async def execute_action(session_id: str, payload: ExecuteActionRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "execute action failed for session %s", session_id):
            with approval_id_errors(payload.approval_id):
                return await manager.execute_decision(
                    session_id,
                    payload.action,
                    approval_id=payload.approval_id,
                )

    @router.post("/sessions/{session_id}/actions/upload")
    async def upload(session_id: str, payload: UploadRequest) -> dict[str, Any]:
        with (
            typed_route_errors(logger, "upload failed for session %s", session_id),
            approval_id_errors(payload.approval_id),
        ):
            return await manager.upload(
                session_id,
                selector=payload.selector,
                element_id=payload.element_id,
                file_path=payload.file_path,
                approved=payload.approved,
                approval_id=payload.approval_id,
            )

    @router.post("/sessions/{session_id}/actions/hover")
    async def hover(session_id: str, payload: HoverRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "hover failed for session %s", session_id):
            return await manager.hover(
                session_id,
                selector=payload.selector,
                element_id=payload.element_id,
                x=payload.x,
                y=payload.y,
            )

    @router.post("/sessions/{session_id}/actions/select-option")
    async def select_option(session_id: str, payload: SelectOptionRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "select option failed for session %s", session_id):
            return await manager.select_option(
                session_id,
                selector=payload.selector,
                element_id=payload.element_id,
                value=payload.value,
                label=payload.label,
                index=payload.index,
            )

    @router.post("/sessions/{session_id}/actions/wait")
    async def wait(session_id: str, payload: WaitRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "wait failed for session %s", session_id):
            return await manager.wait(session_id, payload.wait_ms)

    @router.post("/sessions/{session_id}/actions/reload")
    async def reload(session_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "reload failed for session %s", session_id):
            return await manager.reload(session_id)

    @router.post("/sessions/{session_id}/actions/go-back")
    async def go_back(session_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "go back failed for session %s", session_id):
            return await manager.go_back(session_id)

    @router.post("/sessions/{session_id}/actions/go-forward")
    async def go_forward(session_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "go forward failed for session %s", session_id):
            return await manager.go_forward(session_id)

    @router.post("/sessions/{session_id}/takeover")
    async def request_human_takeover(session_id: str, payload: HumanTakeoverRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "takeover request failed for session %s", session_id):
            return await manager.request_human_takeover(session_id, payload.reason)

    @router.delete("/sessions/{session_id}")
    async def close_session(session_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "close failed for session %s", session_id):
            return await manager.close_session(session_id)

    @router.post("/sessions/{session_id}/fork")
    async def fork_session(
        session_id: str,
        name: str | None = None,
        start_url: str | None = None,
        proxy_persona: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
        user_agent: Annotated[str | None, Query(min_length=1, max_length=2000)] = None,
        protection_mode: ProtectionMode | None = None,
    ) -> dict[str, Any]:
        # Query parameters skip the request models every other route uses, so
        # the URL gets the same validation here.
        with typed_route_errors(logger, "fork failed for session %s", session_id):
            if start_url is not None:
                refuse_another_navigation_host(start_url, field_name="start_url")
                start_url = validate_url(start_url, field_name="start_url", allowed_schemes=HTTP_URL_SCHEMES)
            try:
                # The fork's session reads its proxy persona, as POST /sessions does: an unreadable file is a 503.
                with persona_file_errors():
                    return await manager.fork_session(
                        session_id,
                        name=name,
                        start_url=start_url,
                        proxy_persona=proxy_persona,
                        user_agent=user_agent,
                        protection_mode=protection_mode,
                    )
            except SessionLimitError as exc:
                raise HTTPException(status_code=409, detail=error_text(exc)) from None
            except AuthStateDecryptError as exc:
                # The fork's own stored state, read back by create_session: the fixed text POST /sessions answers.
                raise HTTPException(status_code=422, detail=error_text(exc)) from None
            except RuntimeError as exc:
                # The caller gets the generic answer; the cause is here, and nowhere else, as create's is.
                log_hidden_cause(logger, 409, exc, "fork failed for session %s", session_id)
                raise HTTPException(status_code=409, detail="Conflict") from None
            except RefusedPageError:
                # The page checks' refusal, as the page routes answer it. Every other PermissionError
                # (create_session's URL check among them) stays the generic one.
                raise HTTPException(status_code=403, detail=OFF_LIST_PAGE_DETAIL) from None

    return router
