"""websockify auth plugin: only the noVNC page itself may open the VNC socket.

Chromium runs in this container, so every page it loads can reach noVNC on
127.0.0.1:6080. A WebSocket is not bound by the same-origin policy, and x11vnc
runs without a password by default, so any site the browser visited could open
ws://127.0.0.1:6080/websockify, read the screen (every tab, and in the shared
mode every session) and type into it.

A browser always sends Origin on a WebSocket handshake and page script cannot
change it. The noVNC client is served by this same websockify, so its Origin
names the host it was opened from, which is the Host it connects to. Any other
Origin is refused.

Origin == Host alone would still admit DNS rebinding: a page on
attacker.example:6080 re-points that name at 127.0.0.1 and connects with
matching Origin and Host. So the Host must also be a name this deployment is
reached by: loopback, or one listed in --auth-source. That source carries
NOVNC_ALLOWED_ORIGINS and NOVNC_ALLOWED_HOSTS (space- or comma-separated):
an entry with "://" is an origin to accept even when a proxy rewrote Host, and
its host is allowed; a bare entry is a host name the takeover URL uses.

The Origin check stops a page from opening its own WebSocket. It does not stop a
page from framing http://127.0.0.1:6080/vnc.html: the framed noVNC page opens its
WebSocket with noVNC's own Origin, which passes, and the attacker's page then
sits around a live desktop. Importing this module therefore also makes
websockify's HTTP responses forbid framing (X-Frame-Options and CSP
frame-ancestors). A page can still navigate to noVNC or open it in a window, but
it cannot embed it, and it cannot read it either way: that is cross-origin.
"""

from __future__ import annotations

from urllib.parse import urlsplit

try:
    from websockify.auth_plugins import InvalidOriginError
except ImportError:  # the controller's test suite imports this without websockify

    class InvalidOriginError(Exception):  # type: ignore[no-redef]
        def __init__(self, expected, actual):
            super().__init__(f"Invalid Origin Header: Expected one of {expected}, got {actual!r}")


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

NO_FRAMING_HEADERS = (
    ("X-Frame-Options", "DENY"),
    # Only frame-ancestors: a fuller policy would also govern noVNC's own scripts.
    ("Content-Security-Policy", "frame-ancestors 'none'"),
)


def forbid_framing() -> bool:
    """Add NO_FRAMING_HEADERS to every HTTP response websockify's handler sends.

    Patches the base request handler class rather than ProxyRequestHandler, so it
    holds however websockify was launched (module or script). Idempotent. Returns
    False when websockify is not importable (the controller's test suite).
    """
    try:
        from websockify.websockifyserver import WebSockifyRequestHandler
    except ImportError:
        return False
    if getattr(WebSockifyRequestHandler.end_headers, "_forbids_framing", False):
        return True
    original = WebSockifyRequestHandler.end_headers

    def end_headers(self):
        for name, value in NO_FRAMING_HEADERS:
            self.send_header(name, value)
        original(self)

    end_headers._forbids_framing = True  # type: ignore[attr-defined]
    WebSockifyRequestHandler.end_headers = end_headers
    return True


def _host_name(netloc: str) -> str:
    """The host of a Host header or origin netloc, without port or IPv6 brackets."""
    netloc = netloc.strip().lower()
    if netloc.startswith("["):
        return netloc[1:].partition("]")[0]
    return netloc.rpartition(":")[0] if netloc.count(":") == 1 else netloc


class SameOriginOnly:
    def __init__(self, src=None):
        self.extra_origins: set[str] = set()
        self.allowed_hosts: set[str] = set(LOOPBACK_HOSTS)
        for entry in (src or "").replace(",", " ").split():
            entry = entry.rstrip("/").lower()
            if "://" in entry:
                self.extra_origins.add(entry)
                self.allowed_hosts.add(_host_name(urlsplit(entry).netloc))
            else:
                self.allowed_hosts.add(_host_name(entry))

    def authenticate(self, headers, target_host, target_port):
        origin = (headers.get("Origin") or "").strip().rstrip("/").lower()
        host = (headers.get("Host") or "").strip().lower()
        if origin and origin in self.extra_origins:
            return
        parts = urlsplit(origin)
        if (
            parts.scheme in ("http", "https")
            and host
            and parts.netloc == host
            and _host_name(host) in self.allowed_hosts
        ):
            return
        raise InvalidOriginError(expected=sorted(self.allowed_hosts | self.extra_origins), actual=origin or None)


forbid_framing()
