"""Scope page-level requests to one tab via the ``X-Tab-Id`` header.

Pure ASGI (not BaseHTTPMiddleware) so the context variable it sets is the one
the route handler runs under. Only the page-scoped routes honour the header;
on every other path it is ignored, so session-wide operations (tabs open /
activate / close, session close, auth profiles...) always take the exclusive
session lock exactly as before.
"""

from __future__ import annotations

import json
import re

from starlette.types import ASGIApp, Receive, Scope, Send

from ..browser.tab_scope import current_tab_id, is_valid_tab_id

TAB_SCOPED_ACTIONS = (
    "navigate",
    "click",
    "type",
    "press",
    "dialog",
    "scroll",
    "upload",
    "hover",
    "select-option",
    "wait",
    "reload",
    "go-back",
    "go-forward",
)

_SEGMENT = r"[^/]+"
_TAB_SCOPED_PATHS = (
    (re.compile(rf"^/sessions/{_SEGMENT}/observe/?$"), {"GET", "POST"}),
    (re.compile(rf"^/sessions/{_SEGMENT}/screenshot/?$"), {"POST"}),
    (
        re.compile(rf"^/sessions/{_SEGMENT}/actions/(?:{'|'.join(re.escape(a) for a in TAB_SCOPED_ACTIONS)})/?$"),
        {"POST"},
    ),
    # File transfer (app/file_transfer.py): taking a file from the page, and
    # attaching a pushed file to the page, act on the employee's own tab.
    (re.compile(rf"^/sessions/{_SEGMENT}/files/download/?$"), {"POST"}),
    (re.compile(rf"^/sessions/{_SEGMENT}/files/{_SEGMENT}/attach/?$"), {"POST"}),
)


def is_tab_scoped_path(method: str, path: str) -> bool:
    return any(pattern.match(path) and method.upper() in methods for pattern, methods in _TAB_SCOPED_PATHS)


class TabScopeMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        raw = None
        for name, value in scope.get("headers") or ():
            if name.lower() == b"x-tab-id":
                raw = value
                break
        if raw is None or not is_tab_scoped_path(scope.get("method", "GET"), scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        tab_id = raw.decode("latin-1").strip()
        if not is_valid_tab_id(tab_id):
            body = json.dumps({"detail": "Invalid X-Tab-Id", "code": "invalid_tab_id"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        token = current_tab_id.set(tab_id)
        try:
            await self.app(scope, receive, send)
        finally:
            current_tab_id.reset(token)
