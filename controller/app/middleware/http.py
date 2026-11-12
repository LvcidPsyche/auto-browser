from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.routing import compile_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from ..audit import reset_current_operator, set_current_operator
from ..auth_policy import (
    AUTH_PROFILE_EXPORT_ROUTE,
    BIND_SCOPE_LOOPBACK,
    AuthPolicy,
    Credential,
    credentials_for,
    is_bearer_exempt_path,
    is_loopback_host,
    match_credential,
    policy_for_scope,
    resolve_bind_scope,
    tokenless_loopback_allowed,
)
from ..rate_limits import build_rate_limit_key, is_exempt_path
from ..utils import RefusalLogWindows, held_back_note, quoted_for_log
from .hosts import install_controller_host_middleware, origin_matches_host

logger = logging.getLogger(__name__)

# The writes a page on another site can send a controller with no token, and how a browser says so. Exempt:
# /mesh/receive proves its sender with a signature, and /mcp checks Origin itself against MCP_ALLOWED_ORIGINS
# (_validate_origin), where a browser client on a listed origin is cross-site by definition; /mcp/ is the router's
# redirect to it, which does nothing itself. /mcp/tools/call never reaches that check, so it is not exempt; this list
# names exact paths, not a prefix. Compared relative to root_path, as the router compares them.
_CROSS_SITE_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CROSS_SITE_FETCH_SITES = frozenset({"cross-site", "same-site"})
_CROSS_SITE_EXEMPT_PATHS = frozenset({"/mesh/receive", "/mcp", "/mcp/"})
# GET /auth-profiles/{name}/export writes (an archive, its sidecar, an audit event and a receipt), and a page can send
# it blind: a profile's name can be guessed, where a session id or a share token cannot. Compiled as the router
# compiles it, so what routes to it is what this refuses.
_EXPORT_ROUTE_PATTERN = compile_path(AUTH_PROFILE_EXPORT_ROUTE)[0]
# What works, for whom: this layer runs only where no token is configured, so "send a bearer token" could not work
# here, and a page on another origin could not send one to a controller with a token either (no route answers a CORS
# preflight).
CROSS_SITE_WRITE_DETAIL = (
    "This controller has no API token (API_ALLOW_TOKENLESS_LOOPBACK=true), so it refuses a write sent from another "
    "web origin. A web page must be served from the controller's own origin. A browser extension, or another client "
    "that names an origin, needs the controller to have a token, and sends it as a bearer token."
)
# The three refusals this module logs (a cross-site write, a Host that is no loopback name, a session network peer)
# are answered ahead of the rate limiter, to a page that can send them without end: each kind logs one warning a
# source a window (RefusalLogWindows), and cuts the path or the Host it quotes (quoted_for_log).


# The message uvicorn cancels a request's task with once its graceful-shutdown limit has passed (uvicorn/server.py,
# Server.shutdown); a test fails if the installed uvicorn no longer says it.
UVICORN_SHUTDOWN_CANCEL = "Task cancelled, timeout graceful shutdown exceeded"
_CANCEL_CHAIN_MAX = 16
# How long a stream's closing chunk waits for a client that is not reading: uvicorn's send waits there, without limit,
# for the transport's buffer to drain. uvicorn has by then waited its whole limit for the stream, and it does not
# wait for the tasks it cancels: it runs the lifespan's shutdown next, and when that returns its loop ends and
# cancels what is left, with no message. A client that reads takes the chunk at once. One whose buffer is still full
# gets this second more, so its task and its connection do not stay for the whole cleanup (up to the stop's grace);
# a cleanup shorter than that ends the loop inside the wait, and that cancel is ended quietly as well. Either way the
# body is left unfinished and the layer returns: uvicorn writes one ERROR line, with no traceback ("ASGI callable
# returned without completing response."), and asks the transport to close, which a transport still holding bytes
# its client has not taken does once they are gone, or the process ends.
STREAM_CLOSING_SEND_SECONDS = 1.0


def _is_uvicorns_shutdown_cancel(cancelled: BaseException | None) -> bool:
    """Whether this cancellation is uvicorn's at its shutdown limit, or was raised while that one unwound.

    The layers inside run in anyio task groups, and a group cancelled from outside raises a bare CancelledError of
    its own on the way out: uvicorn's, with its message, is then the one behind it (``__context__``).
    """
    for _ in range(_CANCEL_CHAIN_MAX):
        if cancelled is None:
            return False
        if isinstance(cancelled, asyncio.CancelledError) and UVICORN_SHUTDOWN_CANCEL in cancelled.args:
            return True
        cancelled = cancelled.__context__
    return False


class EventStreamShutdownMiddleware:
    """End an open event stream quietly when uvicorn cancels it at its graceful-shutdown limit.

    An event stream (GET /mcp, GET /sessions/{id}/events) never ends by itself, so at a stop uvicorn waits its limit
    for it and then cancels its task. The cancellation surfaces at the top of the app, where uvicorn logs it as
    "Exception in ASGI application" with a traceback: a normal stop then reads as a crash, once for each open stream.
    Outermost, so everything inside has unwound (the stream's own cleanup has run) and nothing is left for the
    cancellation to stop: the stream's body is finished with its last, empty chunk, and one line says what happened.
    Only that cancellation, and only on a started event stream: any other propagates as it did.

    What has gone out is noted once its send has returned, not when the message is seen: a cancellation can land in
    a message's own send (uvicorn's waits there, before it writes, while the transport is paused), and that message
    is then not out. So a start cancelled in its send is no started stream, and the cancellation propagates, where
    it was answered with a body for a response that had not begun. A stream whose last chunk has gone out, cancelled
    while the app is still in its cleanup, is sent nothing more: a second last chunk is one a strict server refuses.
    That order rests on a send that waits before it writes, as uvicorn's does: a server whose send wrote the last
    chunk and then waited, cancelled in that wait, would be sent a second.

    A client that has gone when the closing chunk is sent has nobody to end the body for. uvicorn's send writes
    nothing to a connection that is lost and raises nothing, so there the line says "ended" (the controller's own
    streams end by themselves when their client goes, and come to no line at all). The OSError branch is for a
    server whose send does raise: that is said in the one line and raised no further, or the quiet stop is a
    traceback again. Any other error of that send is the server's refusal of the chunk, and is raised. A client
    that is there and does not read is waited for STREAM_CLOSING_SEND_SECONDS and no longer, and for it the line
    says "ended" only once the send has returned.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # An event stream's start has gone out; its last chunk has.
        streaming = ended = False

        async def watching(message: Message) -> None:
            nonlocal streaming, ended
            await send(message)
            if message["type"] == "http.response.start":
                content_type = Headers(raw=message.get("headers", [])).get("content-type", "")
                # Lower-cased: a media type is the same in any case, and a stream may write it Text/Event-Stream.
                streaming = content_type.lower().startswith("text/event-stream")
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                ended = True

        try:
            await self.app(scope, receive, watching)
        except asyncio.CancelledError as cancelled:
            if not streaming or not _is_uvicorns_shutdown_cancel(cancelled):
                raise
            path = scope.get("path", "")
            if ended:
                logger.info("the event stream on %r had ended and was still closing at the shutdown limit", path)
                return
            closing: Message = {"type": "http.response.body", "body": b"", "more_body": False}
            try:
                await asyncio.wait_for(send(closing), STREAM_CLOSING_SEND_SECONDS)
            except asyncio.TimeoutError:  # ahead of OSError, which it is a kind of
                logger.info(
                    "the event stream on %r was still open at the shutdown limit, and its client took nothing more "
                    "in %g s: left unfinished",
                    path,
                    STREAM_CLOSING_SEND_SECONDS,
                )
            except asyncio.CancelledError:
                # The loop's own cancel at its end (see STREAM_CLOSING_SEND_SECONDS). Nothing is left to stop here
                # but this wait, and it has stopped: raised, the cancel would be uvicorn's traceback after all.
                logger.info(
                    "the event stream on %r was still open at the shutdown limit, and was cancelled again before "
                    "its client took anything more: left unfinished",
                    path,
                )
            except OSError as gone:
                logger.info(
                    "the event stream on %r was still open at the shutdown limit, and its client had gone (%s)",
                    path,
                    type(gone).__name__,
                )
            else:
                logger.info("the event stream on %r was still open at the shutdown limit: ended", path)


class SessionNetworkPeerMiddleware:
    """Refuse (403) any request whose direct peer is on a docker_ephemeral session network.

    The controller joins every session's network, so each session's browser could
    reach this API at the controller's address there. Browser containers never call
    the controller, so such a request is a page, or code, in a session: it is
    refused before the Host check, the rate limiter, the token check and every
    route. Only the ASGI client address counts, never a forwarded-for header. A
    WebSocket is closed before it is accepted, which the server answers with 403.
    A page can send these without end, so a peer's refusals are logged one a
    window, with the count of the rest in the next line.
    """

    def __init__(self, app: ASGIApp, refuses_peer: Callable[[str], bool]) -> None:
        self.app = app
        self.refuses_peer = refuses_peer
        self._refusal_log = RefusalLogWindows()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        host = client[0] if client else ""
        if not host or not self.refuses_peer(host):
            await self.app(scope, receive, send)
            return
        held_back = self._refusal_log.due(host, now=time.monotonic())
        if held_back is not None:
            # Quoted: a page in the session's browser chooses the path, and a line break in it must not start a line.
            logger.warning(
                "refused a request from session network peer %s to %s%s",
                host,
                quoted_for_log(scope.get("path", "")),
                held_back_note(held_back, "peer"),
            )
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008, reason="session network peer")(scope, receive, send)
            return
        response = JSONResponse(
            status_code=403,
            content={
                "detail": (
                    "Forbidden: this request comes from an isolated browser's session network, and session "
                    "browsers may not call the controller API."
                )
            },
        )
        await response(scope, receive, send)


def install_controller_http_middleware(
    application: FastAPI,
    *,
    settings: Any,
    rate_limiter: Any,
    metrics: Any,
    refuses_peer: Callable[[str], bool] | None = None,
) -> None:
    # Where this API is reachable from is fixed by the deployment, so it is
    # resolved once. Whether a request is authenticated is then one function of
    # that scope and the configured credential — the same function for the auth
    # gate and the rate limiter, rather than each layer re-deciding from the
    # presence of a token (GHSA-xmh3-cw7j-9gp5).
    bind_scope = resolve_bind_scope(settings)
    docs_are_public = bind_scope == BIND_SCOPE_LOOPBACK

    def _is_exempt_path(path: str) -> bool:
        return is_bearer_exempt_path(path, docs_are_public=docs_are_public)

    def _auth_policy() -> AuthPolicy:
        return policy_for_scope(
            bind_scope,
            credentials_for(settings),
            allow_tokenless_loopback=tokenless_loopback_allowed(settings),
        )

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

    def _proved_a_credential(request: Request) -> bool:
        """Whether the request holds a configured credential: what lets the limiter key it by its own headers.

        Under API_ALLOW_TOKENLESS_LOOPBACK every request passes the gate (_has_valid_bearer_token), but no
        credential exists for its Authorization or operator header to prove: keyed by them, a sender that minted a
        new value for each request was never limited, and churned the limiter's buckets (R5C-25).
        """
        return _auth_policy().has_credential and _matched_credential(request) is not None

    @application.middleware("http")
    async def require_api_bearer_token(request: Request, call_next):
        path = _request_path(request)
        if not _auth_policy().required or _is_exempt_path(path):
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
        if rate_limiter is None:
            return await call_next(request)
        # An exempt path skips the limiter only when it needs no token (healthchecks
        # hit it unauthenticated) or the request holds a valid one. /metrics and
        # /artifacts still demand the token, so a tokenless or wrong-token request to
        # them is a guess and counts against the per-IP bucket like /sessions.
        if is_exempt_path(path, settings.request_rate_limit_exempt_path_list) and (
            _is_exempt_path(path) or _has_valid_bearer_token(request)
        ):
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
                authenticated=_proved_a_credential(request),
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
            "/version",
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

        if (
            settings.require_operator_id
            and not path.startswith(exempt_prefixes)
            and not operator_id
            # The token gate inside this layer answers a request with no valid credential with its 401 first.
            and not (_auth_policy().required and credential is None)
        ):
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

    # An API that answers without a credential (API_ALLOW_TOKENLESS_LOOPBACK=true)
    # is safe on loopback only while it also refuses requests addressed to any
    # other name. Otherwise DNS rebinding turns a web page into a same-origin
    # client: the attacker's hostname re-resolves to 127.0.0.1, the browser sends
    # Host: attacker.example, and — with no token to present — the page drives
    # sessions and exports stored logins. The MCP Origin check cannot catch it either, because it accepts the
    # origin the Host header claims. Compose sets CONTROLLER_ALLOWED_HOSTS;
    # running the controller directly left it empty, and empty meant any Host.
    # Evaluated per request against the same policy as the auth gate, so a
    # deployment with a bearer credential is unaffected.
    configured_hosts = list(getattr(settings, "controller_allowed_host_patterns", None) or [])
    refusal_log = RefusalLogWindows()
    host_refusal_log = RefusalLogWindows()  # its own: a client's refused write is not held back by its refused Host

    # Installed before the Host check, so it runs after it: a rebinding hostname is refused there as a Host error, and
    # the refusal here spends no rate-limit budget. Sits with the Host check for the same reason: a page reaches a
    # controller with no token as easily as DNS rebinding does, by writing to it from its own origin. CORS stops a page
    # reading the answer, not sending the write.
    @application.middleware("http")
    async def refuse_cross_site_writes_when_unauthenticated(request: Request, call_next):
        route_path = _route_path(request)
        if (
            _auth_policy().required
            or not _is_a_write(request.method, route_path)
            or route_path in _CROSS_SITE_EXEMPT_PATHS
            or not _comes_from_another_origin(request)
        ):
            return await call_next(request)
        held_back = refusal_log.due(request.client.host if request.client else "unknown", now=time.monotonic())
        if held_back is not None:
            # Quoted: the path is logged decoded, and a line break a page put in it (%0A) must not start a log line.
            logger.warning(
                "refused a write to %s from another web origin%s",
                quoted_for_log(_request_path(request)),
                held_back_note(held_back, "client"),
            )
        return JSONResponse(status_code=403, content={"detail": CROSS_SITE_WRITE_DETAIL})

    @application.middleware("http")
    async def require_loopback_host_when_unauthenticated(request: Request, call_next):
        if configured_hosts or _auth_policy().required or _request_path(request) == "/healthz":
            return await call_next(request)
        host = request.headers.get("host", "")
        if is_loopback_host(host):
            return await call_next(request)
        # A page that rebinds its name sends these as freely as it sends cross-site writes, and chooses the Host.
        held_back = host_refusal_log.due(request.client.host if request.client else "unknown", now=time.monotonic())
        if held_back is not None:
            logger.warning(
                "refused unauthenticated request for non-loopback Host %s%s",
                quoted_for_log(host),
                held_back_note(held_back, "client"),
            )
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    "Invalid host header: this controller runs without an API credential "
                    "(API_ALLOW_TOKENLESS_LOOPBACK=true), so it only answers to loopback host names. "
                    "Set CONTROLLER_ALLOWED_HOSTS to the exact host names clients use (without a token, a "
                    "wildcard there admits no name), or remove API_ALLOW_TOKENLESS_LOOPBACK so the controller "
                    "demands its token."
                )
            },
        )

    # Installed after every layer above, so it runs before them: a request for a
    # name this controller does not answer to is refused as such, not as a 401 or
    # a missing operator id, and never spends rate-limit budget. Metrics stay
    # outside it so the refusal is still counted. It asks the auth gate's own
    # policy, per request, whether a wildcard may admit a Host.
    install_controller_host_middleware(
        application, configured_hosts, requires_credential=lambda: _auth_policy().required
    )

    # Outside the Host check and the auth gate: a peer on a docker_ephemeral session
    # network is refused before anything else answers it (see SessionNetworkPeerMiddleware).
    if refuses_peer is not None:
        application.add_middleware(SessionNetworkPeerMiddleware, refuses_peer=refuses_peer)

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

    # Installed last of the layers that answer, so it also covers the 400/401/429
    # responses the middleware above short-circuits with.
    install_security_headers(application)

    # Outside every layer above, and it answers nothing itself: a cancellation at uvicorn's shutdown limit unwinds
    # them all before it gets here (see EventStreamShutdownMiddleware).
    application.add_middleware(EventStreamShutdownMiddleware)


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


def _route_path(request: Request) -> str:
    """The path as the router matches it: what follows ``root_path``, when the path carries one."""
    path = _request_path(request)
    root_path = str(request.scope.get("root_path") or "")
    if not root_path or not path.startswith(root_path):
        return path
    rest = path[len(root_path) :]
    return rest if not rest or rest.startswith("/") else path


def _is_a_write(method: str, route_path: str) -> bool:
    return method in _CROSS_SITE_WRITE_METHODS or (method == "GET" and bool(_EXPORT_ROUTE_PATTERN.match(route_path)))


def _comes_from_another_origin(request: Request) -> bool:
    """Whether a browser says this request came from another origin. Saying nothing is not saying so (curl, SDKs)."""
    if request.headers.get("sec-fetch-site", "").strip().lower() in _CROSS_SITE_FETCH_SITES:
        return True
    origin = request.headers.get("origin")
    # Present and empty counts as present: only a request with no Origin at all is one no browser marked.
    return origin is not None and not origin_matches_host(origin, request.headers.get("host", ""))
