from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from ..audit import get_current_operator
from ..readiness import run_readiness_checks

# A readiness probe must answer within a probe interval, not within the
# session-creation retry budget (CONNECT_RETRIES x CONNECT_RETRY_DELAY_SECONDS,
# 60s by default). Failing fast is the correct answer for "are you ready?".
READYZ_TIMEOUT_SECONDS = 5.0

if TYPE_CHECKING:
    from ..browser_manager import BrowserManager
    from ..config import Settings
    from ..maintenance import MaintenanceService
    from ..metrics import MetricsRecorder
    from ..orchestrator import BrowserOrchestrator

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEEP_HEALTH_FIXTURE = _REPO_ROOT / "evals" / "fixtures" / "deep_health.html"
_DEEP_HEALTH_FALLBACK = """<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Auto Browser deep health</title></head>
  <body><main data-ab-deep-health="ready">Deep health ready</main></body>
</html>
"""
_DEEP_HEALTH_TIMEOUT_MS = 5_000


async def run_deep_health_probe(manager: "BrowserManager") -> dict[str, Any]:
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []

    fixture_html, fixture_source = _load_deep_health_fixture()
    if 'data-ab-deep-health="ready"' not in fixture_html:
        raise RuntimeError("deep health fixture missing ready marker")
    checks.append(
        {
            "name": "fixture",
            "status": "pass",
            "details": {
                "source": fixture_source,
                "bytes": len(fixture_html.encode("utf-8")),
            },
        }
    )

    browser = await manager.ensure_browser()
    context = await browser.new_context(viewport={"width": 640, "height": 360})
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
        await context.close()

    checks.append({"name": "browser_fixture_render", "status": "pass"})
    return {
        "status": "ok",
        "checks": checks,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _load_deep_health_fixture() -> tuple[str, str]:
    try:
        return _DEEP_HEALTH_FIXTURE.read_text(encoding="utf-8"), str(_DEEP_HEALTH_FIXTURE)
    except OSError:
        logger.warning("deep health fixture unavailable at %s; using embedded fallback", _DEEP_HEALTH_FIXTURE)
        return _DEEP_HEALTH_FALLBACK, "embedded"


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
        except Exception as exc:
            logger.warning("deep health probe failed: %s", exc, exc_info=True)
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
    async def readyz() -> dict[str, str]:
        try:
            # Bounded. ensure_browser() retries CONNECT_RETRIES times (default
            # 60) at CONNECT_RETRY_DELAY_SECONDS apart *while holding the global
            # browser lock*, so with browser-node down this probe used to hang
            # for a minute or more and every concurrent readyz and every
            # create_session queued behind it — the API looked dead instead of
            # reporting "not ready". A readiness probe must answer promptly;
            # cancelling releases the lock via its async context manager.
            await asyncio.wait_for(manager.ensure_browser(), timeout=READYZ_TIMEOUT_SECONDS)
            return {"status": "ready", "environment": settings.environment_name}
        except TimeoutError:
            logger.warning("readiness check timed out after %ss", READYZ_TIMEOUT_SECONDS)
            raise HTTPException(status_code=503, detail="Service unavailable") from None
        except Exception:
            logger.exception("readiness check failed")
            raise HTTPException(status_code=503, detail="Service unavailable") from None

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
        report = run_readiness_checks(settings, mode=mode)
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
