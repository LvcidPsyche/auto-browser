"""The bridge never follows a redirect, so its token never reaches the redirect's target.

urllib re-sent a POST answered 301, 302 or 303 as a GET with the same headers, Authorization included, to
wherever Location pointed. MCP never redirects, so the bridge reports the 3xx as an HTTP error instead.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from auto_browser_client.mcp_bridge import HttpMcpClient

TOKEN = "t" * 43


def _serve(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.seen = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _Elsewhere(BaseHTTPRequestHandler):
    def _record(self) -> None:
        self.server.seen.append((self.command, self.headers.get("Authorization")))  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"jsonrpc": "2.0", "id": 1, "result": {}}')

    do_GET = do_POST = _record

    def log_message(self, *_: object) -> None:
        return


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_reported_and_never_followed(status: int) -> None:
    elsewhere = _serve(_Elsewhere)

    class _Redirecting(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server's naming
            # Read the body first: on Windows a socket closed with unread data resets, and the client's read of
            # this response then fails with WSAECONNABORTED (1 call in 200 before).
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(status)
            self.send_header("Location", f"http://127.0.0.1:{elsewhere.server_port}/collect")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_: object) -> None:
            return

    controller = _serve(_Redirecting)
    try:
        client = HttpMcpClient(base_url=f"http://127.0.0.1:{controller.server_port}/mcp", bearer_token=TOKEN)
        response = client.post_json({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, session_id=None)
    finally:
        controller.shutdown()
        elsewhere.shutdown()

    assert response.status_code == status
    assert elsewhere.seen == []  # type: ignore[attr-defined]
