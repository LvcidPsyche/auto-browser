from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from ..action_errors import RefusedPageError
from ..browser.services.tabs import TabRefusedByAnotherTabError
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
from ._utils import OFF_LIST_OTHER_TAB_DETAIL, OFF_LIST_PAGE_DETAIL, OFF_LIST_TAB_DETAIL, typed_route_errors

logger = logging.getLogger(__name__)


def create_sessions_router(*, manager: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        return await manager.list_sessions()

    @router.post("/sessions")
    async def create_session(payload: CreateSessionRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "create session failed"):
            try:
                return await manager.create_session(
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
            except RuntimeError:
                raise HTTPException(status_code=409, detail="Conflict") from None

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return await manager.get_session_record(session_id)

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
    async def fork_session(session_id: str, name: str | None = None, start_url: str | None = None) -> dict[str, Any]:
        # Query parameters skip the request models every other route uses, so
        # the URL gets the same validation here.
        with typed_route_errors(logger, "fork failed for session %s", session_id):
            if start_url is not None:
                refuse_another_navigation_host(start_url, field_name="start_url")
                start_url = validate_url(start_url, field_name="start_url", allowed_schemes=HTTP_URL_SCHEMES)
            try:
                return await manager.fork_session(session_id, name=name, start_url=start_url)
            except RuntimeError:
                raise HTTPException(status_code=409, detail="Conflict") from None
            except RefusedPageError:
                # The page checks' refusal, as the page routes answer it. Every other PermissionError
                # (create_session's URL check, a fresh page's about:blank among them) stays the generic one.
                raise HTTPException(status_code=403, detail=OFF_LIST_PAGE_DETAIL) from None

    return router
