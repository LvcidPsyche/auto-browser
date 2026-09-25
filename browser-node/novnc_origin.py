"""websockify auth plugin: only the noVNC page itself may open the VNC socket.

Chromium runs in this container, so every page it loads can reach noVNC on
127.0.0.1:6080. A WebSocket is not bound by the same-origin policy, and x11vnc
runs without a password by default, so any site the browser visited could open
ws://127.0.0.1:6080/websockify, read the screen (every tab, and in the shared
mode every session) and type into it.

A browser always sends Origin on a WebSocket handshake and page script cannot
change it. The noVNC client is served by this same websockify, so its Origin
names the host it was opened from, which is the Host it connects to. Any other
Origin is refused, whatever name or tunnel the takeover URL goes through.
Deployments whose proxy rewrites Host can list extra origins in
NOVNC_ALLOWED_ORIGINS (space- or comma-separated), passed as --auth-source.
"""

from __future__ import annotations

from urllib.parse import urlsplit

try:
    from websockify.auth_plugins import InvalidOriginError
except ImportError:  # the controller's test suite imports this without websockify

    class InvalidOriginError(Exception):  # type: ignore[no-redef]
        def __init__(self, expected, actual):
            super().__init__(f"Invalid Origin Header: Expected one of {expected}, got {actual!r}")


class SameOriginOnly:
    def __init__(self, src=None):
        self.extra = {entry.rstrip("/").lower() for entry in (src or "").replace(",", " ").split()}

    def authenticate(self, headers, target_host, target_port):
        origin = (headers.get("Origin") or "").strip().rstrip("/").lower()
        host = (headers.get("Host") or "").strip().lower()
        if origin and origin in self.extra:
            return
        parts = urlsplit(origin)
        if parts.scheme in ("http", "https") and host and parts.netloc == host:
            return
        raise InvalidOriginError(expected=[host] + sorted(self.extra), actual=origin or None)
