from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..audit import reset_current_operator, set_current_operator
from ..auth_policy import (
    AuthPolicy,
    Credential,
    credentials_for,
    is_loopback_host,
    match_credential,
    policy_for_scope,
    resolve_bind_scope,
)
from ..rate_limits import build_rate_limit_key, is_exempt_path

logger = logging.getLogger(__name__)


def install_controller_http_middleware(
    application: FastAPI,
    *,
    settings: Any,
    rate_limiter: Any,
    metrics: Any,
) -> None:
    # Where this API is reachable from is fixed by the deployment, so it is
    # resolved once. Whether a request is authenticated is then one function of
    # that scope and the configured credential — the same function for the auth
    # gate and the rate limiter, rather than each layer re-deciding from the
    # presence of a token (GHSA-xmh3-cw7j-9gp5).
    bind_scope = resolve_bind_scope(settings)

    def _auth_policy() -> AuthPolicy:
        return policy_for_scope(bind_scope, credentials_for(settings))

    def _matched_credential(request: Request) -> Credential | None:
        return match_credential(credentials_for(settings), request.headers.get("authorization", ""))

    def _has_valid_bearer_token(request: Request) -> bool:
        """Whether this request has proven it holds the configured bearer token.

        Used by both the auth gate and the rate limiter. The limiter needs it
        because it runs *outside* the auth gate (deliberately — unauthenticated
        traffic is exactly what must be throttled) and so cannot assume the
        caller is who their headers claim.
        """
        policy = _auth_policy()
        if not policy.required:
            return True
        if not policy.has_credential:
            # Auth is required and there is no credential that could satisfy it.
            # Startup refuses this configuration; if it is somehow reached, the
            # answer is "no", never "everyone".
            return False
        return _matched_credential(request) is not None

    @application.middleware("http")
    async def require_api_bearer_token(request: Request, call_next):
        path = _request_path(request)
        if not _auth_policy().required or _is_bearer_token_exempt_path(path):
            return await call_next(request)

        if not _has_valid_bearer_token(request):
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

        # Key unauthenticated requests by IP only. Deriving the bucket from the
        # authorization header meant every guessed token got a fresh counter, so
        # bearer-token brute force was never throttled at all, and a flood of
        # distinct operator-id headers could evict legitimate buckets from the LRU.
        decision = await rate_limiter.evaluate(
            build_rate_limit_key(
                operator_id_header=settings.operator_id_header,
                headers=request.headers,
                client_host=request.client.host if request.client else None,
                authenticated=_has_valid_bearer_token(request),
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
            "/share/",
        )
        asserted_id = request.headers.get(settings.operator_id_header)
        operator_name = request.headers.get(settings.operator_name_header)

        # A named credential proves who the caller is. The header only ever
        # claims it, so when the two disagree the credential wins and the claim
        # is recorded — a caller asserting someone else's id belongs in the
        # audit trail, not in the identity.
        credential = _matched_credential(request)
        if credential is not None and credential.verifies_identity:
            operator_id = credential.operator_id
            source = "token"
            conflicting = asserted_id if asserted_id and asserted_id != operator_id else None
            if conflicting:
                logger.warning(
                    "operator header %s=%r conflicts with the authenticated operator %r; using the credential",
                    settings.operator_id_header,
                    conflicting,
                    operator_id,
                )
        else:
            operator_id = asserted_id
            source = "header"
            conflicting = None

        if settings.require_operator_id and not path.startswith(exempt_prefixes) and not operator_id:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": f"Missing required operator header: {settings.operator_id_header}",
                },
            )

        token = set_current_operator(
            operator_id,
            name=operator_name,
            source=source,
            asserted_id=conflicting,
        )
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

    # An API that answers without a credential is safe on loopback only while it
    # also refuses requests addressed to any other name. Otherwise DNS rebinding
    # turns a web page into a same-origin client: the attacker's hostname
    # re-resolves to 127.0.0.1, the browser sends Host: attacker.example, and —
    # with no token to present — the page drives sessions and exports stored
    # logins. The MCP Origin check cannot catch it either, because it accepts the
    # origin the Host header claims. Compose sets CONTROLLER_ALLOWED_HOSTS;
    # running the controller directly left it empty, and empty meant any Host.
    # Evaluated per request against the same policy as the auth gate, so a
    # deployment with a bearer credential is unaffected.
    configured_hosts = list(getattr(settings, "controller_allowed_host_patterns", None) or [])

    @application.middleware("http")
    async def require_loopback_host_when_unauthenticated(request: Request, call_next):
        if configured_hosts or _auth_policy().required or _request_path(request) == "/healthz":
            return await call_next(request)
        host = request.headers.get("host", "")
        if is_loopback_host(host):
            return await call_next(request)
        logger.warning("refused unauthenticated request for non-loopback Host %r", host)
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    "Invalid host header: this controller runs without an API credential, so it only answers "
                    "to loopback host names. Set CONTROLLER_ALLOWED_HOSTS to the names clients use, or "
                    "configure API_BEARER_TOKEN."
                )
            },
        )

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

    # Installed last, so it is outermost and also covers the 400/401/429
    # responses the middleware above short-circuits with.
    install_security_headers(application)


_BASELINE_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

# /artifacts serves files the *browser* produced, including downloads whose
# name and bytes a visited site chooses. Served from the controller's origin, a
# downloaded .html or .svg ran script there — and on a tokenless loopback
# controller that script could call the whole API, auth-profile export
# included. `sandbox` without allow-scripts gives the document an opaque origin
# and no script; images and media still render when opened directly.
_ARTIFACT_CSP = "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'"


def install_security_headers(application: FastAPI) -> None:
    @application.middleware("http")
    async def apply_security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in _BASELINE_SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        path = _request_path(request)
        if path == "/artifacts" or path.startswith("/artifacts/"):
            response.headers["Content-Security-Policy"] = _ARTIFACT_CSP
        return response


def _request_path(request: Request) -> str:
    return str(request.scope.get("path") or "")


def _is_bearer_token_exempt_path(path: str) -> bool:
    # /share/{token}/... authenticates with the signed share token instead
    # (app/routes/share.py). POST /sessions/{id}/share, which mints one, is not
    # under this prefix and stays behind the bearer token.
    if path.startswith("/share/"):
        return True
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
