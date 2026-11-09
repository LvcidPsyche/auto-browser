"""Host-header allowlisting for the controller's own HTTP surface.

Starlette's TrustedHostMiddleware takes everything before the first colon as
the host, so an IPv6 Host header such as ``[::1]:8000`` became ``[`` and could
never match. The ``::1`` entry the shipped compose file and ``.env.example`` put
in ``CONTROLLER_ALLOWED_HOSTS`` was therefore dead, and every IPv6 loopback
client was refused. This keeps the same pattern language (exact names,
``*.example.com`` suffixes, ``*``) with a parser that understands brackets.
"""

from __future__ import annotations

import re
from typing import Sequence

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def host_from_header(value: str) -> str:
    """The host part of a Host header, lowercased, without port or IPv6 brackets."""
    candidate = value.strip()
    if candidate.startswith("["):
        host, closed, rest = candidate[1:].partition("]")
        # Only "[v6]" or "[v6]:port" is a Host header. "[::1]evil.com" used to
        # parse as "::1" and so passed as loopback.
        if not closed or (rest and not (rest.startswith(":") and rest[1:].isdigit())):
            return ""
        return host.lower()
    if candidate.count(":") == 1:  # name:port — a bare IPv6 literal has more colons
        candidate = candidate.partition(":")[0]
    return candidate.lower()


# What a browser serializes an Origin to, and nothing looser: a scheme with a default port, a name or a bracketed IPv6
# address, and a port. A path, a query, a user or a second origin in the header is no Origin a browser sent.
_SERIALIZED_ORIGIN = re.compile(r"(https?)://(\[[0-9a-f:.]+\]|[^\[\]/?#@:\s]+)(?::([0-9]{1,5}))?", re.IGNORECASE)
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HTTP_OPTIONAL_WHITESPACE = " \t"


def _origin_key(origin: str) -> tuple[str, int] | None:
    """The host (lowercased, without IPv6 brackets) and port of ``origin``, its scheme's default when it names none."""
    match = _SERIALIZED_ORIGIN.fullmatch(origin)
    if match is None:
        return None
    scheme, host, port = match.groups()
    return host.strip("[]").lower(), int(port) if port else _DEFAULT_PORTS[scheme.lower()]


def origin_matches_host(origin: str, host_header: str) -> bool:
    """Whether an Origin header names the host and port a request's Host header does, as a browser compares them.

    Only host and port: a TLS-terminating proxy leaves the controller on plain HTTP behind an https page. A Host with
    no port means the default port of the scheme the Origin used, so ``http://localhost`` matches ``localhost:80``. An
    Origin or a Host that cannot be parsed matches nothing, which the caller must read as foreign.
    """
    origin = origin.strip(_HTTP_OPTIONAL_WHITESPACE)
    own = _origin_key(origin)
    if own is None:
        return False
    scheme = origin.partition("://")[0]
    return own == _origin_key(f"{scheme}://{host_header.strip(_HTTP_OPTIONAL_WHITESPACE)}")


def _normalize_pattern(pattern: str) -> str:
    normalized = pattern.strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return normalized


def host_is_allowed(host_header: str, patterns: Sequence[str]) -> bool:
    host = host_from_header(host_header)
    if not host:
        return False
    for raw in patterns:
        pattern = _normalize_pattern(raw)
        if pattern == "*":
            return True
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


class ControllerHostMiddleware:
    """Reject requests whose Host header is not in ``allowed_hosts``."""

    def __init__(self, app: ASGIApp, allowed_hosts: Sequence[str]) -> None:
        for pattern in allowed_hosts:
            normalized = _normalize_pattern(pattern)
            wildcard_ok = normalized == "*" or (normalized.startswith("*.") and "*" not in normalized[1:])
            if "*" in normalized and not wildcard_ok:
                raise ValueError(f"invalid host pattern {pattern!r}; wildcards must look like '*.example.com'")
        self.app = app
        self.allowed_hosts = list(allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = Headers(scope=scope).get("host", "")
        if host_is_allowed(host, self.allowed_hosts):
            await self.app(scope, receive, send)
            return
        response = PlainTextResponse("Invalid host header", status_code=400)
        await response(scope, receive, send)


def install_controller_host_middleware(application: FastAPI, allowed_hosts: Sequence[str]) -> None:
    # An empty list is not "allow everything" for an unauthenticated API: the
    # loopback-only Host rule in app/middleware/http.py covers that case.
    if allowed_hosts:
        application.add_middleware(ControllerHostMiddleware, allowed_hosts=list(allowed_hosts))
