from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..audit import get_current_operator
from ..browser_sandbox import browser_sandbox_status
from ..readiness import run_readiness_checks
from ..run_as import controller_user_status
from ..utils import error_text_for_log

# A readiness probe must answer within a probe interval, not within the
# session-creation retry budget (CONNECT_RETRIES x CONNECT_RETRY_DELAY_SECONDS,
# 60s by default). Failing fast is the correct answer for "are you ready?".
READYZ_TIMEOUT_SECONDS = 5.0
# How long a /readyz answer stands, counted from when its probe finished, a failing answer as long as a passing one.
# The route needs no token and is exempt from the rate limit, so a flood must cost one probe per window, an outage
# included. Short: an orchestrator's readiness flips late by at most this much.
READYZ_ANSWER_CACHE_SECONDS = 3.0

if TYPE_CHECKING:
    from ..browser_manager import BrowserManager
    from ..config import Settings
    from ..maintenance import MaintenanceService
    from ..metrics import MetricsRecorder
    from ..orchestrator import BrowserOrchestrator

logger = logging.getLogger(__name__)

# The page the deep probe renders. It is carried here: the image holds only the controller, so no file is read for it.
_DEEP_HEALTH_HTML = """<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Auto Browser deep health</title></head>
  <body><main data-ab-deep-health="ready">Deep health ready</main></body>
</html>
"""
_DEEP_HEALTH_TIMEOUT_MS = 5_000
# The whole probe once the browser is connected (context, page, render), and the extra time its context gets to close.
_DEEP_HEALTH_PROBE_SECONDS = 10.0
_DEEP_HEALTH_CLOSE_SECONDS = 2.0
# The connect a probe started, per manager (by id: the connect holds the manager, so the id is not reused while it
# stands), until it finishes. A probe that finds one waits on it rather than starting another: each timed-out probe
# otherwise left one more connect queued behind the same browser lock, one per poll through an outage.
_PENDING_CONNECTS: dict[int, asyncio.Future[Any]] = {}
# Connects a probe stopped waiting for: a failure then reaches no probe, and is logged.
_UNFINISHED_CONNECTS: set[asyncio.Future[Any]] = set()
# The probe's context closes and context creations it stopped waiting for, held (the loop keeps only weak references to
# tasks) until they end.
_CLOSING: set[asyncio.Future[Any]] = set()


def _shared_connect(manager: "BrowserManager") -> "asyncio.Future[Any]":
    key = id(manager)
    connect = _PENDING_CONNECTS.get(key)
    if connect is None or connect.done():
        connect = asyncio.ensure_future(manager.ensure_browser())
        _PENDING_CONNECTS[key] = connect
        connect.add_done_callback(lambda done: _forget_connect(key, done))
    return connect


def _forget_connect(key: int, connect: "asyncio.Future[Any]") -> None:
    if _PENDING_CONNECTS.get(key) is connect:
        del _PENDING_CONNECTS[key]


def _let_finish_in_background(connect: "asyncio.Future[Any]") -> None:
    if connect in _UNFINISHED_CONNECTS:
        return  # another probe that gave up on it already said so
    _UNFINISHED_CONNECTS.add(connect)
    connect.add_done_callback(_UNFINISHED_CONNECTS.discard)
    connect.add_done_callback(_log_unfinished_connect)


def _log_unfinished_connect(connect: "asyncio.Future[Any]") -> None:
    if not connect.cancelled() and connect.exception() is not None:
        logger.warning(
            "browser connect that outlived a health or readiness probe failed: %s",
            error_text_for_log(connect.exception()),
        )


async def _wait_for_browser(manager: "BrowserManager") -> Any:
    """The browser, waited for at most READYZ_TIMEOUT_SECONDS. The wait is bounded, the connect is not cancelled:
    cancelling it mid-connect leaked a half-acquired browser connection and its gate. It is the connect every probe
    shares (_shared_connect), so polling through an outage queues one connect, not one per probe; it finishes in
    the background, and a failure after the probes gave up on it is logged."""
    connect = _shared_connect(manager)
    try:
        return await asyncio.wait_for(asyncio.shield(connect), timeout=READYZ_TIMEOUT_SECONDS)
    except BaseException:
        if not connect.done():
            _let_finish_in_background(connect)
        raise


class _ReadyzAnswer:
    """The last /readyz answer and the probe that will give the next one.

    Lives on app.state, not this module: the probe is a task on one event loop, and a test or a restart of the
    lifespan must start without it (app/main.py clears it at startup).
    """

    __slots__ = ("probe", "ready", "finished_at")

    def __init__(self) -> None:
        self.probe: "asyncio.Future[bool] | None" = None
        self.ready = False
        self.finished_at: float | None = None


def _readyz_answer(state: Any) -> _ReadyzAnswer:
    answer = getattr(state, "readyz_answer", None)
    if answer is None:
        answer = state.readyz_answer = _ReadyzAnswer()
    return answer


async def _probe_readiness(manager: "BrowserManager", answer: _ReadyzAnswer) -> bool:
    """Run the one probe a window gets. Any failure is a not-ready answer, kept like a ready one and logged once."""
    try:
        # Bounded. ensure_browser() retries CONNECT_RETRIES times (default
        # 60) at CONNECT_RETRY_DELAY_SECONDS apart *while holding the global
        # browser lock*, so with browser-node down this probe used to hang
        # for a minute or more and every concurrent readyz and every
        # create_session queued behind it — the API looked dead instead of
        # reporting "not ready". A readiness probe must answer promptly; it
        # stops waiting, and leaves the shared connect to finish (see
        # _wait_for_browser), rather than cancel it mid-connect.
        await _wait_for_browser(manager)
        ready = True
    except TimeoutError:
        logger.warning("readiness check timed out after %ss", READYZ_TIMEOUT_SECONDS)
        ready = False
    except Exception:
        logger.exception("readiness check failed")
        ready = False
    answer.ready, answer.finished_at = ready, time.monotonic()
    return ready


async def _is_ready(manager: "BrowserManager", answer: _ReadyzAnswer) -> bool:
    if answer.finished_at is not None and time.monotonic() - answer.finished_at < READYZ_ANSWER_CACHE_SECONDS:
        return answer.ready
    if answer.probe is None or answer.probe.done():
        answer.probe = asyncio.ensure_future(_probe_readiness(manager, answer))
    # Shielded: a caller that disconnects or is cancelled ends its own wait, not the probe the others are waiting on.
    return await asyncio.shield(answer.probe)


async def run_deep_health_probe(manager: "BrowserManager") -> dict[str, Any]:
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []

    fixture_html = _DEEP_HEALTH_HTML
    checks.append(
        {
            "name": "fixture",
            "status": "pass",
            "details": {
                "source": "embedded",
                "bytes": len(fixture_html.encode("utf-8")),
            },
        }
    )

    # Bounded like /readyz: a hung ensure_browser() otherwise holds the browser lock and the request open.
    browser = await _wait_for_browser(manager)
    # A connected browser can still stop answering; the render, with its context and page, gets one total.
    await asyncio.wait_for(_render_fixture(browser, fixture_html), timeout=_DEEP_HEALTH_PROBE_SECONDS)

    checks.append({"name": "browser_fixture_render", "status": "pass"})
    return {
        "status": "ok",
        "checks": checks,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


async def _render_fixture(browser: Any, fixture_html: str) -> None:
    context = await _new_context(browser)
    try:
        page = await context.new_page()
        await page.set_content(
            fixture_html,
            wait_until="domcontentloaded",
            timeout=_DEEP_HEALTH_TIMEOUT_MS,
        )
        locator = page.locator("[data-ab-deep-health]")
        marker = await locator.get_attribute("data-ab-deep-health", timeout=_DEEP_HEALTH_TIMEOUT_MS)
        text = await locator.inner_text(timeout=_DEEP_HEALTH_TIMEOUT_MS)
        if marker != "ready" or "Deep health ready" not in text:
            raise RuntimeError("deep health browser probe did not render the ready marker")
    finally:
        await _close_context(context)


async def _new_context(browser: Any) -> Any:
    """A new context. If the probe's total cancels the wait, Chromium may still create it, so it is closed when it does."""
    creating = _hold(asyncio.ensure_future(browser.new_context(viewport={"width": 640, "height": 360})))
    try:
        return await asyncio.shield(creating)
    except BaseException:
        # Also when it is already done: a cancel that lands in the turn of the loop in which Chromium answered finds
        # the context created and handed to nobody. A done future calls the callback soon.
        creating.add_done_callback(_close_late_context)
        raise


def _close_late_context(creating: "asyncio.Future[Any]") -> None:
    if creating.cancelled() or creating.exception() is not None:
        return
    logger.debug("deep health probe: closing a browser context created after the probe gave up")
    _hold(asyncio.ensure_future(creating.result().close())).add_done_callback(_log_failed_close)


def _hold(task: "asyncio.Future[Any]") -> "asyncio.Future[Any]":
    _CLOSING.add(task)
    task.add_done_callback(_CLOSING.discard)
    return task


async def _close_context(context: Any) -> None:
    """Close the probe's context, waiting at most _DEEP_HEALTH_CLOSE_SECONDS; a close that hangs is left to finish,
    also when the probe's own total expires first."""
    closing = _hold(asyncio.ensure_future(context.close()))
    try:
        await asyncio.wait_for(asyncio.shield(closing), timeout=_DEEP_HEALTH_CLOSE_SECONDS)
    except (TimeoutError, asyncio.CancelledError) as stopped:
        logger.warning(
            "deep health probe: closing its browser context took over %ss; left to finish", _DEEP_HEALTH_CLOSE_SECONDS
        )
        closing.add_done_callback(_log_failed_close)
        if isinstance(stopped, asyncio.CancelledError):
            raise


def _log_failed_close(closing: "asyncio.Future[Any]") -> None:
    if not closing.cancelled() and closing.exception() is not None:
        logger.warning(
            "deep health probe: the browser context it gave up closing failed to close: %s",
            error_text_for_log(closing.exception()),
        )


def create_system_router(
    *,
    settings: "Settings",
    manager: "BrowserManager",
    metrics: "MetricsRecorder",
    maintenance: "MaintenanceService",
    orchestrator: "BrowserOrchestrator",
    version: str,
) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/healthz/deep")
    async def healthz_deep() -> JSONResponse:
        try:
            payload = await run_deep_health_probe(manager)
        except TimeoutError:
            logger.warning(
                "deep health probe timed out (browser wait %ss, probe %ss)",
                READYZ_TIMEOUT_SECONDS,
                _DEEP_HEALTH_PROBE_SECONDS,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unhealthy",
                    "environment": settings.environment_name,
                    "error": "deep_health_probe_timed_out",
                },
            )
        except Exception as exc:
            logger.warning("deep health probe failed: %s", error_text_for_log(exc), exc_info=True)
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unhealthy",
                    "environment": settings.environment_name,
                    "error": "deep_health_probe_failed",
                },
            )
        payload["environment"] = settings.environment_name
        return JSONResponse(content=payload)

    @router.get("/readyz")
    async def readyz(request: Request) -> dict[str, str]:
        if await _is_ready(manager, _readyz_answer(request.app.state)):
            return {"status": "ready", "environment": settings.environment_name}
        raise HTTPException(status_code=503, detail="Service unavailable")

    @router.get("/version")
    async def get_version() -> dict[str, str]:
        return {"version": version}

    @router.get("/metrics", include_in_schema=False)
    async def get_metrics() -> Response:
        if not metrics.enabled:
            raise HTTPException(status_code=404, detail="Metrics disabled")
        metrics.set_active_sessions(len(manager.sessions))
        payload, content_type = metrics.render()
        return Response(content=payload, media_type=content_type)

    @router.get("/maintenance/status")
    async def get_maintenance_status() -> dict[str, Any]:
        return {
            "cleanup_on_startup": settings.cleanup_on_startup,
            "cleanup_interval_seconds": settings.cleanup_interval_seconds,
            "artifact_retention_hours": settings.artifact_retention_hours,
            "upload_retention_hours": settings.upload_retention_hours,
            "auth_retention_hours": settings.auth_retention_hours,
            "last_report": maintenance.last_report,
        }

    @router.post("/maintenance/cleanup")
    async def run_maintenance_cleanup() -> dict[str, Any]:
        return await maintenance.run_cleanup()

    @router.get("/readiness")
    async def get_readiness(mode: str = "normal") -> JSONResponse:
        if mode not in {"normal", "confidential"}:
            raise HTTPException(status_code=400, detail="mode must be 'normal' or 'confidential'")
        report = run_readiness_checks(
            settings,
            mode=mode,
            request_gates=manager.request_gates.status(),
            window_births=manager.window_births.status(),
            browser_sandbox=browser_sandbox_status(
                settings,
                manager.runtime_provisioner,
                shared=getattr(getattr(manager, "runtime", None), "shared_sandbox", None),
            ),
            controller_user=controller_user_status(),
        )
        return JSONResponse(
            content=report.to_dict(),
            status_code=200 if report.overall != "fail" else 503,
        )

    @router.get("/agent/providers")
    async def list_agent_providers() -> list[dict[str, Any]]:
        return [item.model_dump() for item in orchestrator.list_providers()]

    @router.get("/operator")
    async def get_operator() -> dict[str, Any]:
        return get_current_operator().model_dump()

    return router
