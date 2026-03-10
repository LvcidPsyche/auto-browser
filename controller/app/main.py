from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .agent_jobs import AgentJobQueue
from .audit import get_current_operator, reset_current_operator, set_current_operator
from .approvals import ApprovalRequiredError
from .browser_manager import BrowserManager
from .config import get_settings
from .maintenance import MaintenanceService
from .mcp_transport import McpHttpTransport
from .metrics import MetricsRecorder
from .models import (
    ApprovalDecisionRequest,
    AgentRunRequest,
    AgentStepRequest,
    ClickRequest,
    CreateSessionRequest,
    HumanTakeoverRequest,
    McpToolCallRequest,
    NavigateRequest,
    PressRequest,
    SaveStorageStateRequest,
    ScrollRequest,
    TypeRequest,
    UploadRequest,
)
from .orchestrator import BrowserOrchestrator
from .provider_registry import ProviderRegistry
from .rate_limits import SlidingWindowRateLimiter, build_rate_limit_key, is_exempt_path
from .runtime_policy import validate_runtime_policy
from .tool_gateway import McpToolGateway

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()
manager = BrowserManager(settings)
providers = ProviderRegistry(settings)
orchestrator = BrowserOrchestrator(manager, providers)
job_queue = AgentJobQueue(
    orchestrator=orchestrator,
    store_root=settings.job_store_root,
    worker_count=settings.agent_job_worker_count,
    audit_store=manager.audit,
)
tool_gateway = McpToolGateway(manager=manager, orchestrator=orchestrator, job_queue=job_queue)
rate_limiter = (
    SlidingWindowRateLimiter(
        limit=settings.request_rate_limit_requests,
        window_seconds=settings.request_rate_limit_window_seconds,
    )
    if settings.request_rate_limit_enabled
    else None
)
metrics = MetricsRecorder()
maintenance = MaintenanceService(settings, session_provider=lambda: manager.sessions.values())
mcp_transport = McpHttpTransport(
    tool_gateway=tool_gateway,
    server_name="auto-browser",
    server_title="Auto Browser MCP",
    server_version="0.2.0",
    allowed_origins=settings.mcp_allowed_origin_list,
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
    await maintenance.startup()
    try:
        yield
    finally:
        await maintenance.shutdown()
        await job_queue.shutdown()
        await manager.shutdown()


app = FastAPI(
    title="Auto Browser Controller",
    version="0.2.0",
    lifespan=lifespan,
    summary="Visual Auto Browser control plane for LLM workflows.",
)

app.mount("/artifacts", StaticFiles(directory=settings.artifact_root), name="artifacts")


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
    try:
        return await job_queue.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}") from exc


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
    try:
        return await manager.get_approval(approval_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown approval: {approval_id}") from exc


@app.post("/approvals/{approval_id}/approve")
async def approve_approval(approval_id: str, payload: ApprovalDecisionRequest) -> dict:
    try:
        return await manager.approve(approval_id, comment=payload.comment)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown approval: {approval_id}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/approvals/{approval_id}/reject")
async def reject_approval(approval_id: str, payload: ApprovalDecisionRequest) -> dict:
    try:
        return await manager.reject(approval_id, comment=payload.comment)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown approval: {approval_id}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/approvals/{approval_id}/execute")
async def execute_approval(approval_id: str) -> dict:
    try:
        return await manager.execute_approval(approval_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown approval: {approval_id}") from exc
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
        )
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
    try:
        return await manager.get_session_record(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc


@app.get("/sessions/{session_id}/auth-state")
async def get_session_auth_state(session_id: str) -> dict:
    try:
        return await manager.get_auth_state_info(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc


@app.get("/sessions/{session_id}/observe")
async def observe(session_id: str, limit: int = 40) -> dict:
    try:
        return await manager.observe(session_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc


@app.post("/sessions/{session_id}/actions/navigate")
async def navigate(session_id: str, payload: NavigateRequest) -> dict:
    try:
        return await manager.navigate(session_id, payload.url)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
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
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
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
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
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
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc


@app.post("/sessions/{session_id}/actions/scroll")
async def scroll(session_id: str, payload: ScrollRequest) -> dict:
    try:
        return await manager.scroll(session_id, payload.delta_x, payload.delta_y)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


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
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/storage-state")
async def save_storage_state(session_id: str, payload: SaveStorageStateRequest) -> dict:
    try:
        return await manager.save_storage_state(session_id, payload.path)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/takeover")
async def request_human_takeover(session_id: str, payload: HumanTakeoverRequest) -> dict:
    try:
        return await manager.request_human_takeover(session_id, payload.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc


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
    try:
        await manager.get_session(session_id)
        return await job_queue.enqueue_step(session_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc


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
    try:
        await manager.get_session(session_id)
        return await job_queue.enqueue_run(session_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc


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


@app.delete("/sessions/{session_id}")
async def close_session(session_id: str) -> dict:
    try:
        return await manager.close_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
