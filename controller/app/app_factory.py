from __future__ import annotations

import errno
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path

import anyio.to_thread
from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from .agent_jobs import AgentJobQueue
from .auth_policy import has_named_credentials
from .browser_manager import BrowserManager
from .config import Settings
from .cron_service import CronService
from .maintenance import MaintenanceService
from .mcp_transport import McpHttpTransport
from .metrics import MetricsRecorder
from .orchestrator import BrowserOrchestrator
from .provider_registry import ProviderRegistry
from .proxy_personas import ProxyPersonaStore
from .rate_limits import SlidingWindowRateLimiter
from .routes._utils import require_session_access
from .routes.system import create_system_router
from .session_ownership import may_use
from .session_share import SessionShareManager
from .tool_gateway import McpToolGateway
from .vision_target import VisionTargeter

logger = logging.getLogger(__name__)

LifespanFactory = Callable[[FastAPI], AbstractAsyncContextManager[None] | AsyncIterator[None]]

# The most characters of a client-supplied path (or of an error that quotes it) one log line carries.
MAX_LOGGED_PATH_CHARS = 200


def _capped(text: str) -> str:
    return text if len(text) <= MAX_LOGGED_PATH_CHARS else text[:MAX_LOGGED_PATH_CHARS] + "..."


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
        session_owner=manager.session_owner,
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


class SessionArtifactFiles(StaticFiles):
    """/artifacts/<session id>/..., served to whoever may use that session.

    A mount is outside the routes' dependencies, and an artifact path names its
    session, so screenshots, traces and downloads of another operator's session
    were one guessed path away (trace.zip always has the same name).

    The session is the first component of the file the path RESOLVES to, never of
    the path as typed: a leading "../" survives Starlette's normalisation, so
    "/artifacts/%2e%2e/artifacts/<session>/trace.zip" typed the session ".." (which
    nobody owns) and still reached <session>'s file.
    """

    def __init__(self, *, directory: str, manager: BrowserManager, require_known_session: bool = False) -> None:
        super().__init__(directory=directory)
        self.manager = manager
        self.require_known_session = require_known_session

    async def get_response(self, path: str, scope: Scope) -> Response:
        # The lookup is blocking file I/O, so it runs in a thread, and its errors are mapped here
        # because this runs ahead of the parent's own lookup, which is where Starlette maps them.
        try:
            session_id = await anyio.to_thread.run_sync(self._session_of, path)
        except ValueError:
            raise HTTPException(status_code=404) from None  # a null byte or other character no path holds
        except OSError as exc:
            if exc.errno != errno.ENAMETOOLONG:  # a name that long is the client's doing, not worth a log line
                # The path is the client's, and Windows says EINVAL (not ENAMETOOLONG) for a name it
                # cannot parse and puts that name in the error too: cap both, or one request writes a
                # log line as long as the request line allows.
                logger.warning("artifact lookup failed for %r: %s", _capped(path), _capped(str(exc)))
            raise HTTPException(status_code=404) from None
        # A path that resolves to nothing is the parent's 404, so nothing is leaked.
        if session_id is not None and not await self._may_serve(session_id):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)

    async def _may_serve(self, session_id: str) -> bool:
        if not self.require_known_session:
            return not session_id or may_use(await self.manager.session_owner(session_id))
        # With named credentials, a first component that is not a session with a record (a file in
        # the artifact root, a directory whose record is gone) has no owner to check, so no one gets it.
        known, owner = await self.manager.session_owner_lookup(session_id)
        return known and may_use(owner)

    def _session_of(self, path: str) -> str | None:
        """The session the path's file belongs to; "" for a file outside any session directory;
        None when the path reaches nothing."""
        full_path, stat_result = self.lookup_path(path)
        if stat_result is None:
            return None
        return self._resolved_session_id(full_path)

    def _resolved_session_id(self, full_path: str) -> str:
        root = Path(os.path.realpath(self.directory))
        try:
            relative = Path(os.path.realpath(full_path)).relative_to(root)
        except ValueError:
            # lookup_path already refuses anything outside the root; fail closed regardless.
            raise HTTPException(status_code=404) from None
        return relative.parts[0] if relative.parts else ""


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
        dependencies=[Depends(require_session_access)],
    )

    application.state.browser_manager = services.manager
    application.state.tool_gateway = services.tool_gateway
    application.state.settings = services.settings

    application.mount(
        "/artifacts",
        SessionArtifactFiles(
            directory=services.settings.artifact_root,
            manager=services.manager,
            require_known_session=has_named_credentials(services.settings),
        ),
        name="artifacts",
    )
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
