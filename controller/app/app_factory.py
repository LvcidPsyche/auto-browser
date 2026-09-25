from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .agent_jobs import AgentJobQueue
from .browser_manager import BrowserManager
from .config import Settings
from .cron_service import CronService
from .maintenance import MaintenanceService
from .mcp_transport import McpHttpTransport
from .metrics import MetricsRecorder
from .middleware.hosts import ControllerHostMiddleware
from .orchestrator import BrowserOrchestrator
from .provider_registry import ProviderRegistry
from .proxy_personas import ProxyPersonaStore
from .rate_limits import SlidingWindowRateLimiter
from .routes.system import create_system_router
from .session_share import SessionShareManager
from .tool_gateway import McpToolGateway
from .vision_target import VisionTargeter

LifespanFactory = Callable[[FastAPI], AbstractAsyncContextManager[None] | AsyncIterator[None]]


@dataclass(slots=True)
class ControllerServices:
    settings: Settings
    proxy_store: ProxyPersonaStore
    manager: BrowserManager
    providers: ProviderRegistry
    orchestrator: BrowserOrchestrator
    job_queue: AgentJobQueue
    cron_service: CronService
    share_manager: SessionShareManager
    vision_targeter: VisionTargeter
    tool_gateway: McpToolGateway
    rate_limiter: SlidingWindowRateLimiter | None
    metrics: MetricsRecorder
    maintenance: MaintenanceService
    mcp_transport: McpHttpTransport


def build_controller_services(settings: Settings, *, version: str) -> ControllerServices:
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
    metrics = MetricsRecorder(enabled=settings.metrics_enabled)
    tool_gateway = McpToolGateway(
        manager=manager,
        orchestrator=orchestrator,
        job_queue=job_queue,
        tool_profile=settings.mcp_tool_profile,
        cron_service=cron_service,
        share_manager=share_manager,
        proxy_store=proxy_store,
        vision_targeter=vision_targeter,
        metrics=metrics,
    )
    rate_limiter = (
        SlidingWindowRateLimiter(
            limit=settings.request_rate_limit_requests,
            window_seconds=settings.request_rate_limit_window_seconds,
            max_buckets=settings.request_rate_limit_max_buckets,
        )
        if settings.request_rate_limit_enabled
        else None
    )
    maintenance = MaintenanceService(settings, session_provider=lambda: manager.sessions.values())
    mcp_transport = McpHttpTransport(
        tool_gateway=tool_gateway,
        server_name="auto-browser",
        server_title="Auto Browser MCP",
        server_version=version,
        allowed_origins=settings.mcp_allowed_origin_list,
        session_store_path=settings.mcp_session_store_path,
        manager=manager,
    )
    return ControllerServices(
        settings=settings,
        proxy_store=proxy_store,
        manager=manager,
        providers=providers,
        orchestrator=orchestrator,
        job_queue=job_queue,
        cron_service=cron_service,
        share_manager=share_manager,
        vision_targeter=vision_targeter,
        tool_gateway=tool_gateway,
        rate_limiter=rate_limiter,
        metrics=metrics,
        maintenance=maintenance,
        mcp_transport=mcp_transport,
    )


def install_controller_host_middleware(application: FastAPI, allowed_hosts: list[str]) -> None:
    # An empty list is not "allow everything" for an unauthenticated API: the
    # loopback-only Host rule in app/middleware/http.py covers that case.
    if allowed_hosts:
        application.add_middleware(ControllerHostMiddleware, allowed_hosts=allowed_hosts)


def create_controller_app(
    *,
    services: ControllerServices,
    version: str,
    lifespan: LifespanFactory,
) -> FastAPI:
    application = FastAPI(
        title="Auto Browser Controller",
        version=version,
        lifespan=lifespan,
        summary="Visual Auto Browser control plane for LLM workflows.",
    )
    install_controller_host_middleware(application, services.settings.controller_allowed_host_patterns)

    application.state.browser_manager = services.manager
    application.state.tool_gateway = services.tool_gateway
    application.state.settings = services.settings

    application.mount("/artifacts", StaticFiles(directory=services.settings.artifact_root), name="artifacts")
    application.include_router(
        create_system_router(
            settings=services.settings,
            manager=services.manager,
            metrics=services.metrics,
            maintenance=services.maintenance,
            orchestrator=services.orchestrator,
            version=version,
        ),
        tags=["system"],
    )

    from .routes.extensions import register_all_routers

    register_all_routers(application)

    return application
