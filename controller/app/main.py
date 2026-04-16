from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.responses import StreamingResponse

from . import events as _events
from .action_errors import BrowserActionError
from .agent_jobs import AgentJobQueue
from .approvals import ApprovalRequiredError
from .audit import get_current_operator, reset_current_operator, set_current_operator
from .browser_manager import BrowserManager
from .config import get_settings
from .cron_service import CronService
from .maintenance import MaintenanceService
from .mcp_transport import McpHttpTransport
from .metrics import MetricsRecorder
from .models import (
    AgentRunRequest,
    AgentStepRequest,
    ApprovalDecisionRequest,
    ClickRequest,
    CreateSessionRequest,
    ExecuteActionRequest,
    HoverRequest,
    HumanTakeoverRequest,
    ImportAuthProfileRequest,
    McpToolCallRequest,
    NavigateRequest,
    ObserveRequest,
    OpenTabRequest,
    PressRequest,
    SaveAuthProfileRequest,
    SaveStorageStateRequest,
    ScreenshotRequest,
    ScrollRequest,
    SelectOptionRequest,
    ShareSessionRequest,
    SocialCommentRequest,
    SocialDmRequest,
    SocialFollowRequest,
    SocialLikeRequest,
    SocialLoginRequest,
    SocialPostRequest,
    SocialRepostRequest,
    SocialScrollRequest,
    SocialSearchRequest,
    SocialUnfollowRequest,
    TabIndexRequest,
    TypeRequest,
    UploadRequest,
    WaitRequest,
)
from .orchestrator import BrowserOrchestrator
from .provider_registry import ProviderRegistry
from .proxy_personas import ProxyPersonaStore
from .rate_limits import SlidingWindowRateLimiter, build_rate_limit_key, is_exempt_path
from .runtime_policy import validate_runtime_policy
from .session_share import SessionShareManager
from .social_errors import SocialActionError
from .tool_gateway import McpToolGateway
from .tool_inputs import CreateCronJobInput, CreateProxyPersonaInput, TriggerCronJobInput
from .vision_target import VisionTargeter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_VERSION = "0.5.4"

settings = get_settings()
proxy_store = ProxyPersonaStore(settings.proxy_persona_file)
manager = BrowserManager(settings, proxy_store=proxy_store)
providers = ProviderRegistry(settings)
orchestrator = BrowserOrchestrator(manager, providers)
job_queue = AgentJobQueue(
    orchestrator=orchestrator,
    store_root=settings.job_store_root,
    worker_count=settings.agent_job_worker_count,
    audit_store=manager.audit,
)
cron_service = CronService(
    store_path=settings.cron_store_path,
    max_jobs=settings.cron_max_jobs,
    job_queue=job_queue,
    manager=manager,
)
share_manager = SessionShareManager(
    secret=settings.share_token_secret,
    ttl_minutes=settings.share_token_ttl_minutes,
)
vision_targeter = VisionTargeter.from_settings(settings)
tool_gateway = McpToolGateway(
    manager=manager,
    orchestrator=orchestrator,
    job_queue=job_queue,
    tool_profile=settings.mcp_tool_profile,
    cron_service=cron_service,
    share_manager=share_manager,
    proxy_store=proxy_store,
    vision_targeter=vision_targeter,
)
rate_limiter = (
    SlidingWindowRateLimiter(
        limit=settings.request_rate_limit_requests,
        window_seconds=settings.request_rate_limit_window_seconds,
    )
    if settings.request_rate_limit_enabled
    else None
)
metrics = MetricsRecorder(enabled=settings.metrics_enabled)
maintenance = MaintenanceService(settings, session_provider=lambda: manager.sessions.values())
mcp_transport = McpHttpTransport(
    tool_gateway=tool_gateway,
    server_name="auto-browser",
    server_title="Auto Browser MCP",
    server_version=_VERSION,
    allowed_origins=settings.mcp_allowed_origin_list,
    session_store_path=settings.mcp_session_store_path,
    manager=manager,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    policy_report = validate_runtime_policy(settings)
    if policy_report.errors:
        raise RuntimeError("Invalid runtime policy:\n- " + "\n- ".join(policy_report.errors))
    for warning in policy_report.warnings:
        logger.warning("runtime policy warning: %s", warning)
    await manager.startup()
    await job_queue.startup()
    await cron_service.startup()
    await maintenance.startup()
    try:
        yield
    finally:
        await maintenance.shutdown()
        await cron_service.shutdown()
        await job_queue.shutdown()
        await manager.shutdown()


app = FastAPI(
    title="Auto Browser Controller",
    version=_VERSION,
    lifespan=lifespan,
    summary="Visual Auto Browser control plane for LLM workflows.",
)

app.mount("/artifacts", StaticFiles(directory=settings.artifact_root), name="artifacts")

# Operator dashboard — served at /ui/
_UI_DIR = Path(__file__).parent / "ui"
if _UI_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")


@app.exception_handler(KeyError)
async def handle_key_not_found(_: Request, exc: KeyError) -> JSONResponse:
    key = exc.args[0] if exc.args else "unknown"
    return JSONResponse(status_code=404, content={"detail": f"Not found: {key}"})


@app.exception_handler(SocialActionError)
async def handle_social_action_error(_: Request, exc: SocialActionError) -> JSONResponse:
    return JSONResponse(status_code=400, content=exc.payload)


@app.exception_handler(BrowserActionError)
async def handle_browser_action_error(_: Request, exc: BrowserActionError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.payload)


@app.middleware("http")
async def require_api_bearer_token(request: Request, call_next):
    if request.url.path in {"/healthz", "/readyz"} or not settings.api_bearer_token:
        return await call_next(request)

    header = request.headers.get("authorization", "")
    expected = f"Bearer {settings.api_bearer_token}"
    if header != expected:
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


@app.middleware("http")
async def enforce_rate_limits(request: Request, call_next):
    if rate_limiter is None or is_exempt_path(request.url.path, settings.request_rate_limit_exempt_path_list):
        return await call_next(request)

    decision = await rate_limiter.evaluate(
        build_rate_limit_key(
            operator_id_header=settings.operator_id_header,
            headers=request.headers,
            client_host=request.client.host if request.client else None,
        )
    )
    headers = {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_after_seconds),
    }
    if decision.exceeded:
        headers["Retry-After"] = str(decision.retry_after_seconds or decision.reset_after_seconds)
        return JSONResponse(
            status_code=429,
            content={
                "detail": "Rate limit exceeded",
                "limit": decision.limit,
                "window_seconds": decision.window_seconds,
                "retry_after_seconds": decision.retry_after_seconds or decision.reset_after_seconds,
            },
            headers=headers,
        )

    response = await call_next(request)
    response.headers.update(headers)
    return response


@app.middleware("http")
async def bind_operator_identity(request: Request, call_next):
    path = request.url.path
    exempt_prefixes = ("/healthz", "/readyz", "/docs", "/openapi.json", "/redoc", "/artifacts", "/metrics")
    operator_id = request.headers.get(settings.operator_id_header)
    operator_name = request.headers.get(settings.operator_name_header)

    if settings.require_operator_id and not path.startswith(exempt_prefixes) and not operator_id:
        return JSONResponse(
            status_code=400,
            content={
                "detail": f"Missing required operator header: {settings.operator_id_header}",
            },
        )

    token = set_current_operator(operator_id, name=operator_name, source="header")
    try:
        return await call_next(request)
    finally:
        reset_current_operator(token)


@app.middleware("http")
async def record_http_metrics(request: Request, call_next):
    if not metrics.enabled:
        return await call_next(request)

    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration = time.perf_counter() - start
        metrics.record_http_request(
            method=request.method,
            path=request.url.path,
            status_code=500,
            duration_seconds=duration,
        )
        raise

    duration = time.perf_counter() - start
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    metrics.record_http_request(
        method=request.method,
        path=path,
        status_code=response.status_code,
        duration_seconds=duration,
    )
    return response


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    try:
        await manager.ensure_browser()
        return {"status": "ready", "environment": settings.environment_name}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/metrics", include_in_schema=False)
async def get_metrics() -> Response:
    if not metrics.enabled:
        raise HTTPException(status_code=404, detail="Metrics disabled")
    metrics.set_active_sessions(len(manager.sessions))
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


@app.get("/maintenance/status")
async def get_maintenance_status() -> dict:
    return {
        "cleanup_on_startup": settings.cleanup_on_startup,
        "cleanup_interval_seconds": settings.cleanup_interval_seconds,
        "artifact_retention_hours": settings.artifact_retention_hours,
        "upload_retention_hours": settings.upload_retention_hours,
        "auth_retention_hours": settings.auth_retention_hours,
        "last_report": maintenance.last_report,
    }


@app.post("/maintenance/cleanup")
async def run_maintenance_cleanup() -> dict:
    return await maintenance.run_cleanup()


@app.get("/agent/providers")
async def list_agent_providers() -> list[dict]:
    return [item.model_dump() for item in orchestrator.list_providers()]


@app.get("/operator")
async def get_operator() -> dict:
    return get_current_operator().model_dump()


@app.get("/agent/jobs")
async def list_agent_jobs(status: str | None = None, session_id: str | None = None) -> list[dict]:
    return await job_queue.list_jobs(status=status, session_id=session_id)


@app.get("/agent/jobs/{job_id}")
async def get_agent_job(job_id: str) -> dict:
    return await job_queue.get_job(job_id)


@app.get("/remote-access")
async def get_remote_access(session_id: str | None = None) -> dict:
    if session_id and session_id not in manager.sessions:
        try:
            record = await manager.get_session_record(session_id)
            return record["remote_access"]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    return manager.get_remote_access_info(session_id)


@app.get("/audit/events")
async def list_audit_events(
    limit: int = 100,
    session_id: str | None = None,
    event_type: str | None = None,
    operator_id: str | None = None,
) -> list[dict]:
    return await manager.list_audit_events(
        limit=max(1, min(limit, 500)),
        session_id=session_id,
        event_type=event_type,
        operator_id=operator_id,
    )


@app.get("/approvals")
async def list_approvals(status: str | None = None, session_id: str | None = None) -> list[dict]:
    return await manager.list_approvals(status=status, session_id=session_id)


@app.get("/approvals/{approval_id}")
async def get_approval(approval_id: str) -> dict:
    return await manager.get_approval(approval_id)


@app.post("/approvals/{approval_id}/approve")
async def approve_approval(approval_id: str, payload: ApprovalDecisionRequest) -> dict:
    try:
        return await manager.approve(approval_id, comment=payload.comment)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/approvals/{approval_id}/reject")
async def reject_approval(approval_id: str, payload: ApprovalDecisionRequest) -> dict:
    try:
        return await manager.reject(approval_id, comment=payload.comment)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/approvals/{approval_id}/execute")
async def execute_approval(approval_id: str) -> dict:
    try:
        return await manager.execute_approval(approval_id)
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/sessions")
async def list_sessions() -> list[dict]:
    return await manager.list_sessions()


@app.post("/sessions")
async def create_session(payload: CreateSessionRequest) -> dict:
    try:
        return await manager.create_session(
            name=payload.name,
            start_url=payload.start_url,
            storage_state_path=payload.storage_state_path,
            auth_profile=payload.auth_profile,
            proxy_persona=payload.proxy_persona,
            request_proxy_server=payload.proxy_server,
            request_proxy_username=payload.proxy_username,
            request_proxy_password=payload.proxy_password,
            user_agent=payload.user_agent,
            protection_mode=payload.protection_mode,
            totp_secret=payload.totp_secret,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    return await manager.get_session_record(session_id)


@app.get("/sessions/{session_id}/auth-state")
async def get_session_auth_state(session_id: str) -> dict:
    return await manager.get_auth_state_info(session_id)


@app.get("/auth-profiles")
async def list_auth_profiles() -> list[dict]:
    return await manager.list_auth_profiles()


@app.get("/auth-profiles/{profile_name}")
async def get_auth_profile(profile_name: str) -> dict:
    try:
        return await manager.get_auth_profile(profile_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/sessions/{session_id}/observe")
async def observe(session_id: str, limit: int = 40, preset: str = "normal") -> dict:
    return await manager.observe(session_id, limit=limit, preset=preset)


@app.post("/sessions/{session_id}/observe")
async def observe_post(session_id: str, payload: ObserveRequest) -> dict:
    """Observe with a perception preset. POST body allows richer options than query params."""
    return await manager.observe(session_id, limit=payload.limit, preset=payload.preset)


@app.post("/sessions/{session_id}/screenshot")
async def capture_screenshot(session_id: str, payload: ScreenshotRequest) -> dict:
    return await manager.capture_screenshot(session_id, label=payload.label)


@app.get("/sessions/{session_id}/downloads")
async def list_downloads(session_id: str) -> list[dict]:
    return await manager.list_downloads(session_id)


@app.get("/sessions/{session_id}/tabs")
async def list_tabs(session_id: str) -> list[dict]:
    return await manager.list_tabs(session_id)


@app.post("/sessions/{session_id}/tabs/activate")
async def activate_tab(session_id: str, payload: TabIndexRequest) -> dict:
    try:
        return await manager.activate_tab(session_id, payload.index)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/tabs/close")
async def close_tab(session_id: str, payload: TabIndexRequest) -> dict:
    try:
        return await manager.close_tab(session_id, payload.index)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/tabs/open")
async def open_tab(session_id: str, payload: OpenTabRequest) -> dict:
    try:
        return await manager.open_tab(session_id, payload.url, payload.activate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/actions/navigate")
async def navigate(session_id: str, payload: NavigateRequest) -> dict:
    try:
        return await manager.navigate(session_id, payload.url)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc


@app.post("/sessions/{session_id}/actions/click")
async def click(session_id: str, payload: ClickRequest) -> dict:
    try:
        return await manager.click(
            session_id,
            selector=payload.selector,
            element_id=payload.element_id,
            x=payload.x,
            y=payload.y,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc


@app.post("/sessions/{session_id}/actions/type")
async def type_text(session_id: str, payload: TypeRequest) -> dict:
    try:
        return await manager.type(
            session_id,
            selector=payload.selector,
            element_id=payload.element_id,
            text=payload.text,
            clear_first=payload.clear_first,
            sensitive=payload.sensitive,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc


@app.post("/sessions/{session_id}/actions/press")
async def press_key(session_id: str, payload: PressRequest) -> dict:
    try:
        return await manager.press(session_id, payload.key)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc


@app.post("/sessions/{session_id}/actions/scroll")
async def scroll(session_id: str, payload: ScrollRequest) -> dict:
    try:
        return await manager.scroll(session_id, payload.delta_x, payload.delta_y)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/actions/execute")
async def execute_action(session_id: str, payload: ExecuteActionRequest) -> dict:
    try:
        return await manager.execute_decision(
            session_id,
            payload.action,
            approval_id=payload.approval_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc


@app.post("/sessions/{session_id}/actions/upload")
async def upload(session_id: str, payload: UploadRequest) -> dict:
    try:
        return await manager.upload(
            session_id,
            selector=payload.selector,
            element_id=payload.element_id,
            file_path=payload.file_path,
            approved=payload.approved,
            approval_id=payload.approval_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/actions/hover")
async def hover(session_id: str, payload: HoverRequest) -> dict:
    try:
        return await manager.hover(
            session_id,
            selector=payload.selector,
            element_id=payload.element_id,
            x=payload.x,
            y=payload.y,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc


@app.post("/sessions/{session_id}/actions/select-option")
async def select_option(session_id: str, payload: SelectOptionRequest) -> dict:
    try:
        return await manager.select_option(
            session_id,
            selector=payload.selector,
            element_id=payload.element_id,
            value=payload.value,
            label=payload.label,
            index=payload.index,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc


@app.post("/sessions/{session_id}/actions/wait")
async def wait(session_id: str, payload: WaitRequest) -> dict:
    return await manager.wait(session_id, payload.wait_ms)


@app.post("/sessions/{session_id}/actions/reload")
async def reload(session_id: str) -> dict:
    try:
        return await manager.reload(session_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc


@app.post("/sessions/{session_id}/actions/go-back")
async def go_back(session_id: str) -> dict:
    try:
        return await manager.go_back(session_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc


@app.post("/sessions/{session_id}/actions/go-forward")
async def go_forward(session_id: str) -> dict:
    try:
        return await manager.go_forward(session_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc


@app.post("/sessions/{session_id}/social/scroll")
async def social_scroll_feed(session_id: str, payload: SocialScrollRequest) -> dict:
    return await manager.scroll_feed(session_id, direction=payload.direction, screens=payload.screens)


@app.get("/sessions/{session_id}/social/posts")
async def social_extract_posts(session_id: str, limit: int = 20) -> list:
    return await manager.extract_posts(session_id, limit=limit)


@app.get("/sessions/{session_id}/social/comments")
async def social_extract_comments(session_id: str, limit: int = 20) -> list:
    return await manager.extract_comments(session_id, limit=limit)


@app.get("/sessions/{session_id}/social/profile")
async def social_extract_profile(session_id: str) -> dict:
    return await manager.extract_profile(session_id)


@app.post("/sessions/{session_id}/social/post")
async def social_post(session_id: str, payload: SocialPostRequest) -> dict:
    try:
        return await manager.post_content(session_id, text=payload.text, approval_id=payload.approval_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/social/comment")
async def social_comment(session_id: str, payload: SocialCommentRequest) -> dict:
    try:
        return await manager.comment_on_post(
            session_id,
            text=payload.text,
            post_index=payload.post_index,
            approval_id=payload.approval_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/social/like")
async def social_like(session_id: str, payload: SocialLikeRequest) -> dict:
    try:
        return await manager.like_post(session_id, post_index=payload.post_index, approval_id=payload.approval_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/social/follow")
async def social_follow(session_id: str, payload: SocialFollowRequest) -> dict:
    try:
        return await manager.follow_user(session_id, approval_id=payload.approval_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/social/unfollow")
async def social_unfollow(session_id: str, payload: SocialUnfollowRequest) -> dict:
    try:
        return await manager.unfollow_user(session_id, approval_id=payload.approval_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/social/repost")
async def social_repost(session_id: str, payload: SocialRepostRequest) -> dict:
    try:
        return await manager.repost_post(
            session_id,
            post_index=payload.post_index,
            approval_id=payload.approval_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/social/dm")
async def social_dm(session_id: str, payload: SocialDmRequest) -> dict:
    try:
        return await manager.send_direct_message(
            session_id,
            recipient=payload.recipient,
            text=payload.text,
            approval_id=payload.approval_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/social/login")
async def social_login(session_id: str, payload: SocialLoginRequest) -> dict:
    try:
        return await manager.social_login(
            session_id,
            platform=payload.platform,
            username=payload.username,
            password=payload.password,
            auth_profile=payload.auth_profile,
            approval_id=payload.approval_id,
            totp_secret=payload.totp_secret,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/social/search")
async def social_search(session_id: str, payload: SocialSearchRequest) -> dict:
    try:
        return await manager.search_page(session_id, query=payload.query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/storage-state")
async def save_storage_state(session_id: str, payload: SaveStorageStateRequest) -> dict:
    try:
        return await manager.save_storage_state(session_id, payload.path)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/auth-profiles")
async def save_auth_profile(session_id: str, payload: SaveAuthProfileRequest) -> dict:
    try:
        return await manager.save_auth_profile(session_id, payload.profile_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/takeover")
async def request_human_takeover(session_id: str, payload: HumanTakeoverRequest) -> dict:
    return await manager.request_human_takeover(session_id, payload.reason)


@app.post("/sessions/{session_id}/agent/step")
async def run_agent_step(session_id: str, payload: AgentStepRequest) -> dict:
    try:
        result = await orchestrator.step(
            session_id=session_id,
            provider_name=payload.provider,
            goal=payload.goal,
            observation_limit=payload.observation_limit,
            context_hints=payload.context_hints,
            upload_approved=payload.upload_approved,
            approval_id=payload.approval_id,
            provider_model=payload.provider_model,
        )
        status_code = 200 if result.status != "error" else (result.error_code or 502)
        if status_code != 200:
            raise HTTPException(status_code=status_code, detail=result.model_dump())
        return result.model_dump()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/agent/jobs/step", status_code=202)
async def enqueue_agent_step(session_id: str, payload: AgentStepRequest) -> dict:
    await manager.get_session(session_id)
    return await job_queue.enqueue_step(session_id, payload)


@app.post("/sessions/{session_id}/agent/run")
async def run_agent_loop(session_id: str, payload: AgentRunRequest) -> dict:
    try:
        result = await orchestrator.run(
            session_id=session_id,
            provider_name=payload.provider,
            goal=payload.goal,
            max_steps=payload.max_steps,
            observation_limit=payload.observation_limit,
            context_hints=payload.context_hints,
            upload_approved=payload.upload_approved,
            approval_id=payload.approval_id,
            provider_model=payload.provider_model,
        )
        return result.model_dump()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/agent/jobs/run", status_code=202)
async def enqueue_agent_run(session_id: str, payload: AgentRunRequest) -> dict:
    await manager.get_session(session_id)
    return await job_queue.enqueue_run(session_id, payload)


@app.get("/mcp")
async def get_mcp_transport(request: Request):
    return await mcp_transport.handle_get_request(request)


@app.post("/mcp")
async def post_mcp_transport(request: Request):
    return await mcp_transport.handle_post_request(request)


@app.delete("/mcp")
async def delete_mcp_transport(request: Request):
    return await mcp_transport.handle_delete_request(request)


@app.get("/mcp/tools")
async def list_mcp_tools() -> list[dict]:
    return tool_gateway.list_tools()


@app.post("/mcp/tools/call")
async def call_mcp_tool(payload: McpToolCallRequest) -> dict:
    return (await tool_gateway.call_tool(payload)).model_dump()


@app.get("/sessions/{session_id}/events")
async def session_events(session_id: str, request: Request):
    """SSE stream of observe/action/approval events for a session.

    Clients receive newline-delimited ``data: <json>\\n\\n`` messages.
    A keepalive comment is sent every ``settings.sse_keepalive_seconds`` seconds.
    """
    await manager.get_session(session_id)

    queue = _events.subscribe(session_id)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(
                        queue.get(),
                        timeout=settings.sse_keepalive_seconds,
                    )
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    # keepalive comment — prevents proxy from dropping idle connection
                    yield ": keepalive\n\n"
        finally:
            _events.unsubscribe(session_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/sessions/{session_id}/screenshot/compare")
async def screenshot_compare(session_id: str) -> dict:
    """Capture a screenshot and diff it against the most recent prior screenshot.

    Returns pixel change count, percentage, and a diff image URL.
    """
    try:
        return await manager.screenshot_diff(session_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/sessions/{session_id}/replay", response_class=HTMLResponse)
async def session_replay(session_id: str) -> HTMLResponse:
    """Session replay — HTML viewer showing screenshots, audit events, and approvals."""
    import html as _html

    # Gather screenshots
    artifact_dir = Path(settings.artifact_root) / session_id
    screenshots: list[tuple[str, str]] = []  # (url, label)
    if artifact_dir.is_dir():
        for f in sorted(artifact_dir.glob("*.png")):
            label = f.stem.replace("-", " ")
            screenshots.append((f"/artifacts/{session_id}/{f.name}", label))

    # Gather audit events for this session
    try:
        events = await manager.list_audit_events(session_id=session_id, limit=200)
    except Exception:
        events = []

    # Gather session info
    session_info: dict = {}
    try:
        session = manager.sessions.get(session_id)
        if session:
            session_info = await manager._session_summary(session)
        else:
            record = await manager.session_store.get(session_id)
            session_info = record.model_dump()
    except Exception:
        pass

    def esc(s: object) -> str:
        return _html.escape(str(s or ""))

    screenshots_html = "".join(
        f'<figure><img src="{esc(url)}" loading="lazy"><figcaption>{esc(lbl)}</figcaption></figure>'
        for url, lbl in screenshots
    ) or "<p class=muted>No screenshots captured yet.</p>"

    events_html = "".join(
        f'<tr><td class=muted>{esc(e.get("timestamp","")[:19])}</td>'
        f'<td>{esc(e.get("event_type",""))}</td>'
        f'<td>{esc(e.get("operator_id",""))}</td>'
        f'<td>{esc(str(e.get("data",""))[:120])}</td></tr>'
        for e in events
    ) or '<tr><td colspan=4 class=muted>No audit events.</td></tr>'

    status = esc(session_info.get("status", "unknown"))
    current_url = esc(session_info.get("url", ""))
    title = esc(session_info.get("title", session_id))
    created = esc(str(session_info.get("created_at", ""))[:19])

    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Replay — {esc(session_id)}</title>
<style>
  :root {{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;--accent:#58a6ff}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;padding:24px}}
  h1{{font-size:18px;font-weight:600;margin-bottom:4px}}
  h2{{font-size:14px;font-weight:600;margin:24px 0 12px;border-bottom:1px solid var(--border);padding-bottom:6px}}
  .meta{{color:var(--muted);font-size:12px;margin-bottom:20px}}
  .meta span{{margin-right:16px}}
  .gallery{{display:flex;flex-wrap:wrap;gap:12px}}
  figure{{background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;max-width:340px}}
  figure img{{width:100%;display:block}}
  figcaption{{font-size:11px;color:var(--muted);padding:6px 8px}}
  table{{width:100%;border-collapse:collapse;font-size:12px}}
  th{{text-align:left;padding:6px 8px;color:var(--muted);border-bottom:1px solid var(--border);font-weight:500}}
  td{{padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:top;word-break:break-word}}
  .muted{{color:var(--muted)}}
  a{{color:var(--accent);text-decoration:none}}
</style>
</head>
<body>
<h1>Session Replay</h1>
<div class="meta">
  <span>ID: <strong>{esc(session_id)}</strong></span>
  <span>Status: <strong>{status}</strong></span>
  <span>Created: {created}</span>
  <span>Title: {title}</span>
  {f'<span>URL: <a href="{current_url}" target="_blank">{current_url}</a></span>' if current_url else ''}
</div>
<h2>Screenshots ({len(screenshots)})</h2>
<div class="gallery">{screenshots_html}</div>
<h2>Audit Events ({len(events)})</h2>
<table>
  <thead><tr><th>Time</th><th>Type</th><th>Operator</th><th>Data</th></tr></thead>
  <tbody>{events_html}</tbody>
</table>
</body>
</html>"""
    return HTMLResponse(content=body)


@app.get("/auth-profiles/{profile_name}/export")
async def export_auth_profile(profile_name: str):
    """Download an auth profile as a .tar.gz archive."""
    try:
        result = await manager.export_auth_profile(profile_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    archive_path = Path(result["archive_path"])
    if not archive_path.exists():
        raise HTTPException(status_code=500, detail="archive file not found after export")

    return FileResponse(
        path=str(archive_path),
        media_type="application/gzip",
        filename=result["archive_name"],
    )


@app.post("/auth-profiles/import")
async def import_auth_profile(payload: ImportAuthProfileRequest) -> dict:
    """Import an auth profile from a .tar.gz archive on the server filesystem."""
    try:
        return await manager.import_auth_profile(payload.archive_path, overwrite=payload.overwrite)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/sessions/{session_id}")
async def close_session(session_id: str) -> dict:
    return await manager.close_session(session_id)


# ── v0.5.0 endpoints ───────────────────────────────────────────────────────

@app.get("/sessions/{session_id}/network-log")
async def get_network_log(
    session_id: str,
    limit: int = 100,
    method: str | None = None,
    url_contains: str | None = None,
) -> dict:
    return await manager.get_network_log(
        session_id, limit=limit, method=method, url_contains=url_contains
    )


@app.post("/sessions/{session_id}/fork")
async def fork_session(session_id: str, name: str | None = None, start_url: str | None = None) -> dict:
    try:
        return await manager.fork_session(session_id, name=name, start_url=start_url)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/share")
async def share_session(session_id: str, payload: ShareSessionRequest | None = None) -> dict:
    try:
        await manager.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc

    ttl_minutes = payload.ttl_minutes if payload is not None else 60
    try:
        return share_manager.create_token(session_id, ttl_seconds=ttl_minutes * 60)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/share/{token}/observe")
async def shared_observe(token: str) -> dict:
    """Read-only observe endpoint accessible via share token."""
    info = share_manager.token_info(token)
    if not info.get("valid"):
        raise HTTPException(status_code=403, detail=info.get("error", "Invalid token"))
    try:
        return await manager.observe(info["session_id"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {info['session_id']}") from exc


@app.get("/share/{token}", response_class=HTMLResponse)
async def shared_session_view(token: str) -> HTMLResponse:
    """Lightweight observer page for a shared session token."""
    info = share_manager.token_info(token)
    if not info.get("valid"):
        raise HTTPException(status_code=403, detail=info.get("error", "Invalid token"))
    try:
        await manager.get_session(info["session_id"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {info['session_id']}") from exc

    token_json = json.dumps(token)
    session_id_json = json.dumps(info["session_id"])
    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Shared Session {info["session_id"]}</title>
    <style>
      :root {{
        color-scheme: dark;
        --bg: #111418;
        --panel: #1a2027;
        --panel-border: #2b3440;
        --text: #f5f7fa;
        --muted: #9aa6b2;
        --accent: #7dd3fc;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: ui-sans-serif, system-ui, sans-serif;
        background:
          radial-gradient(circle at top, rgba(125, 211, 252, 0.16), transparent 28%),
          linear-gradient(180deg, #0b0f14, var(--bg));
        color: var(--text);
      }}
      main {{
        max-width: 1180px;
        margin: 0 auto;
        padding: 24px;
      }}
      header {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 18px;
      }}
      h1 {{
        margin: 0;
        font-size: 1.2rem;
      }}
      .meta {{
        color: var(--muted);
        font-size: 0.95rem;
      }}
      .panel {{
        background: rgba(26, 32, 39, 0.92);
        border: 1px solid var(--panel-border);
        border-radius: 18px;
        overflow: hidden;
        box-shadow: 0 18px 40px rgba(0, 0, 0, 0.24);
      }}
      .toolbar {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: center;
        justify-content: space-between;
        padding: 14px 18px;
        border-bottom: 1px solid var(--panel-border);
      }}
      .status {{
        color: var(--muted);
        font-size: 0.95rem;
      }}
      .status strong {{
        color: var(--accent);
      }}
      .frame {{
        aspect-ratio: 16 / 10;
        width: 100%;
        background: #0b0f14;
        display: flex;
        align-items: center;
        justify-content: center;
      }}
      img {{
        width: 100%;
        height: 100%;
        object-fit: contain;
        display: block;
      }}
      .error {{
        padding: 24px;
        color: #fecaca;
      }}
      a {{
        color: var(--accent);
      }}
    </style>
  </head>
  <body>
    <main>
      <header>
        <div>
          <h1>Shared Session Observer</h1>
          <div class="meta">Session <code id="session-id"></code></div>
        </div>
        <div class="meta" id="url">Waiting for first snapshot…</div>
      </header>
      <section class="panel">
        <div class="toolbar">
          <div class="status"><strong id="state">Connecting</strong> <span id="detail">Fetching shared observe payload…</span></div>
          <div class="meta" id="updated">Never updated</div>
        </div>
        <div class="frame" id="frame">
          <div class="meta">Loading screenshot…</div>
        </div>
      </section>
    </main>
    <script>
      const token = {token_json};
      const sessionId = {session_id_json};
      const observeUrl = `/share/${{token}}/observe`;
      const imageEl = document.createElement("img");
      const frameEl = document.getElementById("frame");
      const stateEl = document.getElementById("state");
      const detailEl = document.getElementById("detail");
      const updatedEl = document.getElementById("updated");
      const urlEl = document.getElementById("url");
      document.getElementById("session-id").textContent = sessionId;

      async function refresh() {{
        try {{
          const response = await fetch(observeUrl, {{ cache: "no-store" }});
          if (!response.ok) {{
            throw new Error(`HTTP ${{response.status}}`);
          }}
          const payload = await response.json();
          imageEl.src = `${{payload.screenshot_url}}?ts=${{Date.now()}}`;
          imageEl.alt = payload.title || payload.url || sessionId;
          if (!imageEl.isConnected) {{
            frameEl.replaceChildren(imageEl);
          }}
          stateEl.textContent = "Live";
          detailEl.textContent = payload.title || "Shared observe payload loaded";
          urlEl.innerHTML = payload.url ? `<a href="${{payload.url}}" target="_blank" rel="noreferrer">${{payload.url}}</a>` : "No URL available";
          updatedEl.textContent = `Updated ${{new Date().toLocaleTimeString()}}`;
        }} catch (error) {{
          frameEl.innerHTML = `<div class="error">Unable to refresh shared session: ${{error.message}}</div>`;
          stateEl.textContent = "Error";
          detailEl.textContent = "Retrying every 5 seconds";
          updatedEl.textContent = `Last attempt ${{new Date().toLocaleTimeString()}}`;
        }}
      }}

      refresh();
      setInterval(refresh, 5000);
    </script>
  </body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/sessions/{session_id}/shadow-browse")
async def enable_shadow_browse(session_id: str) -> dict:
    """Launch a headed browser session for debugging."""
    try:
        return await manager.enable_shadow_browse(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/sessions/{session_id}/audit")
async def get_session_audit(
    session_id: str,
    limit: int = 200,
    event_type: str | None = None,
) -> dict:
    """Return audit events for a session as JSON, newest first."""
    events = await manager.audit.list(
        session_id=session_id,
        limit=min(limit, 5000),
        event_type=event_type or None,
    )
    return {
        "session_id": session_id,
        "count": len(events),
        "events": [e.model_dump() for e in events],
    }


@app.get("/sessions/{session_id}/witness")
async def get_session_witness(session_id: str, limit: int = 100) -> dict:
    receipts = await manager.list_witness_receipts(session_id, limit=min(limit, 5000))
    return {
        "session_id": session_id,
        "count": len(receipts),
        "receipts": receipts,
    }


@app.get("/sessions/{session_id}/export-script")
async def export_script(session_id: str) -> dict:
    """Export session actions as a runnable Playwright Python script."""
    from .playwright_export import export_session_script
    session = await manager.get_session(session_id)
    return await export_session_script(
        session_id,
        manager.audit,
        start_url=session.page.url,
        viewport_w=settings.default_viewport_width,
        viewport_h=settings.default_viewport_height,
    )


@app.get("/sessions/{session_id}/trace")
async def get_trace(session_id: str) -> dict:
    """Return trace file metadata and download URL."""
    from pathlib import Path as _Path
    session = await manager.get_session(session_id)
    trace_path = _Path(str(session.trace_path)) if hasattr(session, "trace_path") else None
    if trace_path and trace_path.exists():
        return {
            "session_id": session_id,
            "trace_path": str(trace_path),
            "trace_url": f"/artifacts/{session_id}/{trace_path.name}",
            "trace_size_bytes": trace_path.stat().st_size,
            "viewer_url": f"https://trace.playwright.dev/?trace=/artifacts/{session_id}/{trace_path.name}",
        }
    return {"session_id": session_id, "trace_path": None, "trace_url": None, "viewer_url": None}


@app.get("/pii-scrubber")
async def get_pii_scrubber() -> dict:
    """Return PII scrubber configuration."""
    return manager.get_pii_scrubber_status()


# ── Proxy persona endpoints ─────────────────────────────────────────────────

@app.get("/proxy-personas")
async def list_proxy_personas() -> list:
    return proxy_store.list_personas()


@app.post("/proxy-personas")
async def set_proxy_persona(payload: CreateProxyPersonaInput) -> dict:
    try:
        return proxy_store.set_persona(
            payload.name,
            server=payload.server,
            username=payload.username,
            password=payload.password,
            description=payload.description,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/proxy-personas/{name}")
async def get_proxy_persona(name: str) -> dict:
    try:
        return proxy_store.get_persona(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/proxy-personas/{name}")
async def delete_proxy_persona(name: str) -> dict:
    deleted = proxy_store.delete_persona(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Proxy persona not found: {name!r}")
    return {"deleted": True, "name": name}


# ── Cron endpoints ─────────────────────────────────────────────────────────

@app.get("/crons")
async def list_cron_jobs() -> list:
    return await cron_service.list_jobs()


@app.post("/crons")
async def create_cron_job(payload: CreateCronJobInput) -> dict:
    try:
        return await cron_service.create_job(
            name=payload.name,
            goal=payload.goal,
            provider=payload.provider,
            schedule=payload.schedule,
            start_url=payload.start_url,
            auth_profile=payload.auth_profile,
            proxy_persona=payload.proxy_persona,
            max_steps=payload.max_steps,
            enabled=payload.enabled,
            webhook_enabled=payload.webhook_enabled,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/crons/{job_id}")
async def get_cron_job(job_id: str) -> dict:
    return await cron_service.get_job(job_id)


@app.delete("/crons/{job_id}")
async def delete_cron_job(job_id: str) -> dict:
    deleted = await cron_service.delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Cron job not found: {job_id}")
    return {"deleted": True, "job_id": job_id}


@app.post("/crons/{job_id}/trigger")
async def trigger_cron_job_via_webhook(job_id: str, request: Request) -> dict:
    """Webhook endpoint — requires webhook_key in request body."""
    try:
        body = await request.json()
        payload = TriggerCronJobInput.model_validate({"job_id": job_id, **body})
        return await cron_service.trigger_via_webhook(payload.job_id, payload.webhook_key or "")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
