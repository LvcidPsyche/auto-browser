from __future__ import annotations

import hmac
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..audit import reset_current_operator, set_current_operator
from ..rate_limits import build_rate_limit_key, is_exempt_path


def install_controller_http_middleware(
    application: FastAPI,
    *,
    settings: Any,
    rate_limiter: Any,
    metrics: Any,
) -> None:
    @application.middleware("http")
    async def require_api_bearer_token(request: Request, call_next):
        path = _request_path(request)
        if not settings.api_bearer_token or _is_bearer_token_exempt_path(path):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        expected = f"Bearer {settings.api_bearer_token}"
        if not hmac.compare_digest(header.encode(), expected.encode()):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @application.middleware("http")
    async def enforce_rate_limits(request: Request, call_next):
        path = _request_path(request)
        if rate_limiter is None or is_exempt_path(path, settings.request_rate_limit_exempt_path_list):
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

    @application.middleware("http")
    async def bind_operator_identity(request: Request, call_next):
        path = _request_path(request)
        exempt_prefixes = (
            "/healthz",
            "/readyz",
            "/docs",
            "/openapi.json",
            "/redoc",
            "/artifacts",
            "/metrics",
            "/dashboard",
            "/ui",
            "/mesh/receive",
        )
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

    def _metric_path(request: Request) -> str:
        """Prometheus label for a request path — always a bounded value.

        Only the matched route *template* may become a label. Using the raw URL
        path meant unmatched requests each minted their own label, so on a public
        MCP endpoint ordinary scanner traffic grew the metrics registry without
        bound for the lifetime of the process. Unmatched requests all collapse
        into one bucket instead.
        """
        route = request.scope.get("route")
        template = getattr(route, "path", None)
        return template or "__unmatched__"

    @application.middleware("http")
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
                path=_metric_path(request),
                status_code=500,
                duration_seconds=duration,
            )
            raise

        duration = time.perf_counter() - start
        path = _metric_path(request)
        metrics.record_http_request(
            method=request.method,
            path=path,
            status_code=response.status_code,
            duration_seconds=duration,
        )
        return response


def _request_path(request: Request) -> str:
    return str(request.scope.get("path") or "")


def _is_bearer_token_exempt_path(path: str) -> bool:
    return path in {
        "/healthz",
        "/readyz",
        "/mesh/receive",
        "/version",
        "/dashboard",
        "/dashboard/",
        "/ui",
        "/ui/",
    }
