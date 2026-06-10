from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .action_errors import BrowserActionError
from .app_factory import (
    build_controller_services,
    create_controller_app,
    install_controller_host_middleware,
)
from .compliance import apply_compliance_template, write_compliance_manifest
from .config import get_settings
from .middleware import install_controller_http_middleware
from .routes.agent import create_agent_router
from .routes.auth_profiles import create_auth_profiles_router
from .routes.mcp import create_mcp_router
from .routes.operations import create_operations_router
from .routes.session_diagnostics import create_session_diagnostics_router
from .routes.sessions import create_sessions_router
from .routes.share import create_share_router
from .runtime_policy import validate_runtime_policy

_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(level=_log_level)
logger = logging.getLogger(__name__)

_VERSION = "1.2.1"

def _install_controller_host_middleware(application: FastAPI, allowed_hosts: list[str]) -> None:
    install_controller_host_middleware(application, allowed_hosts)


settings = get_settings()
_compliance_template = settings.compliance_template.strip() if settings.compliance_template else None
_compliance_overrides: dict[str, object] | None = None
if _compliance_template:
    try:
        _compliance_overrides = apply_compliance_template(settings, _compliance_template)
    except ValueError as exc:
        raise RuntimeError(f"Invalid COMPLIANCE_TEMPLATE={_compliance_template!r}: {exc}") from exc
services = build_controller_services(settings, version=_VERSION)
proxy_store = services.proxy_store
manager = services.manager
providers = services.providers
orchestrator = services.orchestrator
job_queue = services.job_queue
cron_service = services.cron_service
share_manager = services.share_manager
tool_gateway = services.tool_gateway
rate_limiter = services.rate_limiter
metrics = services.metrics
maintenance = services.maintenance
mcp_transport = services.mcp_transport


@asynccontextmanager
async def lifespan(application: FastAPI):
    if _compliance_template and _compliance_overrides is not None:
        write_compliance_manifest(
            template_name=_compliance_template,
            overrides=_compliance_overrides,
            output_path=Path(settings.compliance_manifest_path),
        )
        logger.info("compliance template applied: %s", _compliance_template)
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
        from .startup.extensions import register_extensions

        register_extensions(application)
    except Exception as exc:
        logger.error("v1.0 extensions init failed (non-fatal): %s", exc)
    try:
        yield
    finally:
        await maintenance.shutdown()
        await cron_service.shutdown()
        await job_queue.shutdown()
        await manager.shutdown()


app = create_controller_app(services=services, version=_VERSION, lifespan=lifespan)
app.include_router(create_mcp_router(mcp_transport=mcp_transport, tool_gateway=tool_gateway))
app.include_router(create_agent_router(manager=manager, orchestrator=orchestrator, job_queue=job_queue))
app.include_router(create_auth_profiles_router(manager=manager, settings=settings))
app.include_router(create_session_diagnostics_router(manager=manager, settings=settings))
app.include_router(create_sessions_router(manager=manager))
app.include_router(create_share_router(manager=manager, share_manager=share_manager))
app.include_router(
    create_operations_router(
        manager=manager,
        proxy_store=proxy_store,
        cron_service=cron_service,
    )
)
install_controller_http_middleware(app, settings=settings, rate_limiter=rate_limiter, metrics=metrics)

# Legacy operator dashboard aliases now redirect to the auth-bootstrap-aware dashboard.
@app.get("/ui", include_in_schema=False)
@app.get("/ui/", include_in_schema=False)
@app.get("/ui/{rest_of_path:path}", include_in_schema=False)
async def legacy_ui_redirect(_: Request, rest_of_path: str = "") -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=307)


@app.exception_handler(KeyError)
async def handle_key_not_found(_: Request, exc: KeyError) -> JSONResponse:
    key = exc.args[0] if exc.args else "unknown"
    return JSONResponse(status_code=404, content={"detail": f"Not found: {key}"})


@app.exception_handler(BrowserActionError)
async def handle_browser_action_error(_: Request, exc: BrowserActionError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.payload)
