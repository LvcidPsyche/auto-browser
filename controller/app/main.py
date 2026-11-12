from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException

from . import process_hardening
from .action_errors import BrowserActionError, SessionNotFoundError
from .api_token import ensure_api_credential, ensure_takeover_token
from .app_factory import build_controller_services, create_controller_app
from .auth_policy import has_named_credentials
from .compliance import apply_compliance_template, write_compliance_manifest
from .config import get_settings
from .middleware import install_controller_http_middleware
from .proxy_personas import warn_about_ownerless_personas
from .routes._utils import log_hidden_cause
from .routes.agent import create_agent_router
from .routes.auth_profiles import create_auth_profiles_router
from .routes.mcp import create_mcp_router
from .routes.operations import create_operations_router
from .routes.session_diagnostics import create_session_diagnostics_router
from .routes.sessions import TakeoverRefusalWindows, create_sessions_router, write_held_takeover_refusals
from .routes.share import create_share_router
from .runtime_policy import validate_runtime_policy
from .safe_json_response import SafeJSONResponse, safe_http_exception_handler, safe_validation_exception_handler
from .utils import capped_for_log, error_text_for_log
from .version import __version__ as _VERSION

_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(level=_log_level)
# httpx logs every request's whole URL at INFO, userinfo, path and query included: an approval webhook's URL is often
# its credential, and a provider's can carry one. Its warnings and errors still come through.
logging.getLogger("httpx").setLevel(max(_log_level, logging.WARNING))
logger = logging.getLogger(__name__)


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
# The takeover-link refusals that are only counted. The route counts them and writes them at the source's next call;
# the maintenance sweep writes those of a window that is over, and the shutdown below writes the rest.
takeover_refusals = TakeoverRefusalWindows()
maintenance.write_held_refusals = partial(write_held_takeover_refusals, manager, takeover_refusals, finished_only=True)


@asynccontextmanager
async def lifespan(application: FastAPI):
    # Before anything starts a child (W2-27): a process of the controller's uid can read a dumpable controller's
    # whole environment through /proc/PID/environ. Through the module, so the test suite can keep itself dumpable.
    process_hardening.make_non_dumpable()
    # Before anything can run OCR (W5-1): pytesseract starts Tesseract with the controller's whole environment.
    process_hardening.confine_tesseract_environment()
    # First, so the runtime policy below sees a token loaded or generated from
    # API_TOKEN_FILE. Not at import: tests and dev harnesses import this module.
    ensure_api_credential(settings)
    # After it, so a token loaded from API_TOKEN_FILE counts as an agent's credential the takeover token must differ from.
    ensure_takeover_token(settings)
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
    warn_about_ownerless_personas(proxy_store, named_tokens=has_named_credentials(settings))
    # A start of the lifespan has no /readyz answer yet, and no probe in flight on a closed event loop.
    application.state.readyz_answer = None
    await manager.startup()
    await job_queue.startup()
    await cron_service.startup()
    await maintenance.startup()
    try:
        from .startup.extensions import register_extensions

        register_extensions(application)
        maintenance.harness_service = getattr(application.state, "harness_service", None)
    except Exception as exc:
        logger.error("v1.0 extensions init failed (non-fatal): %s", error_text_for_log(exc))
    try:
        yield
    finally:
        await maintenance.shutdown()
        await cron_service.shutdown()
        await job_queue.shutdown()
        await write_held_takeover_refusals(manager, takeover_refusals)
        await manager.shutdown()


app = create_controller_app(services=services, version=_VERSION, lifespan=lifespan)
# Tags group the endpoints on /docs; untagged, all ~90 landed in one "default" list.
app.include_router(create_mcp_router(mcp_transport=mcp_transport, tool_gateway=tool_gateway), tags=["mcp"])
app.include_router(create_agent_router(manager=manager, orchestrator=orchestrator, job_queue=job_queue), tags=["agent"])
app.include_router(create_auth_profiles_router(manager=manager, settings=settings), tags=["auth profiles"])
app.include_router(create_session_diagnostics_router(manager=manager, settings=settings), tags=["diagnostics"])
app.include_router(create_sessions_router(manager=manager, refusal_windows=takeover_refusals), tags=["sessions"])
app.include_router(
    create_share_router(manager=manager, share_manager=share_manager, settings=settings), tags=["sharing"]
)
app.include_router(
    create_operations_router(
        manager=manager,
        proxy_store=proxy_store,
        cron_service=cron_service,
    ),
    tags=["operations"],
)
install_controller_http_middleware(
    app,
    settings=settings,
    rate_limiter=rate_limiter,
    metrics=metrics,
    # A request from a docker_ephemeral session network is refused (403) before auth.
    refuses_peer=manager.runtime_provisioner.peer_is_in_session_network,
)


# Legacy operator dashboard aliases now redirect to the auth-bootstrap-aware dashboard.
@app.get("/ui", include_in_schema=False)
@app.get("/ui/", include_in_schema=False)
@app.get("/ui/{rest_of_path:path}", include_in_schema=False)
async def legacy_ui_redirect(_: Request, rest_of_path: str = "") -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=307)


@app.exception_handler(KeyError)
async def handle_key_not_found(request: Request, exc: KeyError) -> SafeJSONResponse:
    if isinstance(exc, SessionNotFoundError):
        # reason: the controller's own close (request_gate_lost), for a client to branch on; absent otherwise.
        content = {"detail": exc.message, "code": exc.code, **({"reason": exc.reason} if exc.reason else {})}
        return SafeJSONResponse(status_code=404, content=content)
    # A plain KeyError is either an unknown id a service chose to raise as one or a bug
    # reaching the client as a 404; the log line is the only way to tell them apart.
    # The path is the caller's, decoded, and so is the key the error quotes as often as not (an id from the path):
    # a control character in either must not reach the log as itself, and each is as long as the server lets a
    # request line be. So the error is written as a route's hidden cause is (log_hidden_cause: quoted, cut, with the
    # frames nearest the raise) and not by exc_info, which prints its text whole; the answer cuts the key as well.
    log_hidden_cause(logger, 404, exc, "unmapped KeyError on %s %s", request.method, request.url.path)
    key = exc.args[0] if exc.args else "unknown"
    return SafeJSONResponse(status_code=404, content={"detail": f"Not found: {capped_for_log(str(key))}"})


@app.exception_handler(BrowserActionError)
async def handle_browser_action_error(_: Request, exc: BrowserActionError) -> SafeJSONResponse:
    return SafeJSONResponse(status_code=exc.status_code, content=exc.payload)


app.add_exception_handler(HTTPException, safe_http_exception_handler)
app.add_exception_handler(RequestValidationError, safe_validation_exception_handler)
