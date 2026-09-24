"""Host-header allowlisting for the controller's own HTTP surface.

Starlette's TrustedHostMiddleware takes everything before the first colon as
the host, so an IPv6 Host header such as ``[::1]:8000`` became ``[`` and could
never match. The ``::1`` entry the shipped compose file and ``.env.example`` put
in ``CONTROLLER_ALLOWED_HOSTS`` was therefore dead, and every IPv6 loopback
client was refused. This keeps the same pattern language (exact names,
``*.example.com`` suffixes, ``*``) with a parser that understands brackets.
"""

from __future__ import annotations

from typing import Sequence

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def host_from_header(value: str) -> str:
    """The host part of a Host header, lowercased, without port or IPv6 brackets."""
    candidate = value.strip()
    if candidate.startswith("["):
        return candidate[1:].partition("]")[0].lower()
    if candidate.count(":") == 1:  # name:port — a bare IPv6 literal has more colons
        candidate = candidate.partition(":")[0]
    return candidate.lower()


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
