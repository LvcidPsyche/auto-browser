from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from ..action_errors import RefusedPageError
from ..api_token import TAKEOVER_LINK_UNAVAILABLE_DETAIL
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
from ..url_safety import refuse_another_navigation_host
from ._utils import (
    OFF_LIST_OTHER_TAB_DETAIL,
    OFF_LIST_PAGE_DETAIL,
    OFF_LIST_TAB_DETAIL,
    TAKEOVER_LINK_ROUTE,
    UNJUDGED_TAB_DETAIL,
    persona_file_errors,
    require_safe_segment,
    typed_route_errors,
)

logger = logging.getLogger(__name__)

# One text for a missing and a wrong takeover token, and for a session that exists and one that does not. It names
# only the header, not where the token lives: every caller without the token reads it (W2-22).
TAKEOVER_LINK_REFUSED_DETAIL = "This endpoint needs the takeover token in the X-Takeover-Token header."


def create_sessions_router(*, manager: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
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
                raise HTTPException(status_code=409, detail=str(exc)) from None
            except AuthStateDecryptError as exc:
                # A fixed text that says what to check; it names no path.
                raise HTTPException(status_code=422, detail=str(exc)) from None
            except RuntimeError:
                raise HTTPException(status_code=409, detail="Conflict") from None
            return created

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return await manager.get_session_record(session_id)

    @router.get(TAKEOVER_LINK_ROUTE)
    async def get_takeover_link(
        session_id: str,
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
        if not hmac.compare_digest(presented, expected.encode("utf-8")):
            await manager.audit.append(
                event_type="takeover_link_refused",
                status="refused",
                action="get_takeover_link",
                session_id=session_id,
                details={"reason": "missing takeover token" if not x_takeover_token else "wrong takeover token"},
            )
            raise HTTPException(status_code=403, detail=TAKEOVER_LINK_REFUSED_DETAIL)
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
        with typed_route_errors(logger, "observe failed for session %s", session_id):
            try:
                return await manager.observe(session_id, limit=limit, preset=preset)
            except PermissionError:
                raise HTTPException(status_code=403, detail=OFF_LIST_PAGE_DETAIL) from None

    @router.post("/sessions/{session_id}/observe")
    async def observe_post(session_id: str, payload: ObserveRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "observe failed for session %s", session_id):
            try:
                return await manager.observe(session_id, limit=payload.limit, preset=payload.preset)
            except PermissionError:
                raise HTTPException(status_code=403, detail=OFF_LIST_PAGE_DETAIL) from None

    @router.post("/sessions/{session_id}/screenshot")
    async def capture_screenshot(session_id: str, payload: ScreenshotRequest) -> dict[str, Any]:
        try:
            return await manager.capture_screenshot(session_id, label=payload.label)
        except PermissionError:
            raise HTTPException(status_code=403, detail=OFF_LIST_PAGE_DETAIL) from None

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
        try:
            return await manager.activate_tab(session_id, payload.index)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid request") from None
        except TabRefusedByAnotherTabError:
            raise HTTPException(status_code=403, detail=OFF_LIST_OTHER_TAB_DETAIL) from None
        except TabUnjudgedError:
            raise HTTPException(status_code=403, detail=UNJUDGED_TAB_DETAIL) from None
        except PermissionError:
            raise HTTPException(status_code=403, detail=OFF_LIST_TAB_DETAIL) from None

    @router.post("/sessions/{session_id}/tabs/close")
    async def close_tab(session_id: str, payload: TabIndexRequest) -> dict[str, Any]:
        try:
            return await manager.close_tab(session_id, payload.index)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid request") from None

    @router.post("/sessions/{session_id}/tabs/open")
    async def open_tab(session_id: str, payload: OpenTabRequest) -> dict[str, Any]:
        try:
            return await manager.open_tab(session_id, payload.url, payload.activate)
        except PermissionError:
            raise HTTPException(status_code=403, detail="Not permitted") from None
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid request") from None

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

    @router.post("/sessions/{session_id}/actions/execute")
    async def execute_action(session_id: str, payload: ExecuteActionRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "execute action failed for session %s", session_id):
            return await manager.execute_decision(
                session_id,
                payload.action,
                approval_id=payload.approval_id,
            )

    @router.post("/sessions/{session_id}/actions/upload")
    async def upload(session_id: str, payload: UploadRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "upload failed for session %s", session_id):
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
                raise HTTPException(status_code=409, detail=str(exc)) from None
            except RuntimeError:
                raise HTTPException(status_code=409, detail="Conflict") from None
            except RefusedPageError:
                # The page checks' refusal, as the page routes answer it. Every other PermissionError
                # (create_session's URL check among them) stays the generic one.
                raise HTTPException(status_code=403, detail=OFF_LIST_PAGE_DETAIL) from None

    return router
