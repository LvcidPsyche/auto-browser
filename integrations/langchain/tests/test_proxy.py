"""The tool and node send a loopback controller's token past any environment proxy, never to the proxy (W7-1).

httpx follows HTTP_PROXY, http_proxy and ALL_PROXY to a loopback host when NO_PROXY does not name it, so the
Authorization header for a local controller went to the proxy. A loopback base URL now gets a transport of its
own (httpx uses no environment proxy when one is given); any other host still goes through the proxy.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from auto_browser_langchain import AutoBrowserNode, AutoBrowserTool

MARKER = "m" * 43


class _Recording(BaseHTTPRequestHandler):
    def _answer(self, payload: object) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append((self.path, self.headers.get("Authorization")))  # type: ignore[attr-defined]
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        self._answer([])

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        self._answer({"content": [{"text": "ok"}]})

    def log_message(self, *_: object) -> None:
        return


def _serve() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recording)
    server.seen = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def servers(monkeypatch):
    # Each server's close registered as it starts (W12-7): a second bind that fails leaves no listener behind.
    with contextlib.ExitStack() as stack:
        proxy = _serve()
        stack.callback(_stop, proxy)
        controller = _serve()
        stack.callback(_stop, controller)
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        for name in ("NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        monkeypatch.setenv("http_proxy", proxy_url)
        yield proxy, controller


def _stop(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def _calls(base_url: str) -> None:
    tool = AutoBrowserTool(base_url=base_url, bearer_token=MARKER)
    assert tool._run("browser.observe", {}) == "ok"
    assert AutoBrowserTool.list_tools(base_url=base_url, bearer_token=MARKER) == []
    node = AutoBrowserNode(base_url=base_url, bearer_token=MARKER)
    assert asyncio.run(node.call_tool("browser.observe", {})) == {"content": [{"text": "ok"}]}


def test_a_loopback_controller_is_reached_directly(servers) -> None:
    proxy, controller = servers
    _calls(f"http://127.0.0.1:{controller.server_port}")
    assert proxy.seen == []
    assert controller.seen == [
        ("/mcp/tools/call", f"Bearer {MARKER}"),
        ("/mcp/tools", f"Bearer {MARKER}"),
        ("/mcp/tools/call", f"Bearer {MARKER}"),
    ]


def test_localhost_is_reached_directly(servers) -> None:
    proxy, controller = servers
    tool = AutoBrowserTool(base_url=f"http://localhost:{controller.server_port}", bearer_token=MARKER)
    try:
        assert tool._run("browser.observe", {}) == "ok"
    except httpx.ConnectError:  # localhost resolved only to ::1 here, where the controller does not listen
        # W11-8: only the refused direct connection skips; a failure before the request is sent fails the test.
        pytest.skip("localhost does not reach 127.0.0.1 on this host")
    assert proxy.seen == []


@pytest.mark.parametrize("host", ["0.0.0.0", "[::]", "localhost."])
def test_a_bind_all_or_trailing_dot_host_never_goes_through_the_proxy(servers, host: str) -> None:
    # The direct request may fail where nothing answers on that address (0.0.0.0 on Windows, :: on a host without
    # IPv6); the assertion is what the proxy saw, which holds everywhere.
    proxy, controller = servers
    base_url = f"http://{host}:{controller.server_port}"
    calls = (
        lambda: AutoBrowserTool(base_url=base_url, bearer_token=MARKER)._run("browser.observe", {}),
        lambda: AutoBrowserTool.list_tools(base_url=base_url, bearer_token=MARKER),
        lambda: asyncio.run(AutoBrowserNode(base_url=base_url, bearer_token=MARKER).call_tool("browser.observe", {})),
    )
    for call in calls:
        try:
            call()
        except httpx.ConnectError:  # W10-16: only the refused direct connection, never a failure before sending
            pass
    assert proxy.seen == []


@pytest.mark.parametrize("host", ["127.2", "2130706434", "0x7f000002", "[::ffff:127.0.0.2]"])
def test_w9_5_a_loopback_address_written_another_way_never_goes_through_the_proxy(servers, host: str) -> None:
    # httpx picked the environment proxy for these, while the socket layer connects to 127.0.0.2 for each. Nothing
    # listens on 127.0.0.2, so the direct request fails; the assertion is what the proxy saw.
    if not host.startswith("["):
        try:
            socket.getaddrinfo(host, 80, flags=socket.AI_NUMERICHOST)
        except OSError:
            pytest.skip(f"this platform's numeric parser does not read {host}, so no socket reaches it directly")
    proxy, controller = servers
    base_url = f"http://{host}:{controller.server_port}"
    calls = (
        lambda: AutoBrowserTool(base_url=base_url, bearer_token=MARKER)._run("browser.observe", {}),
        lambda: AutoBrowserTool.list_tools(base_url=base_url, bearer_token=MARKER),
        lambda: asyncio.run(AutoBrowserNode(base_url=base_url, bearer_token=MARKER).call_tool("browser.observe", {})),
    )
    # W10-16: each fails on the refused direct connection, never before sending (as W10-14's TypeError did).
    for call in calls:
        with pytest.raises(httpx.ConnectError):
            call()
    assert proxy.seen == []


def test_a_remote_controller_still_goes_through_the_proxy(servers) -> None:
    proxy, controller = servers
    _calls("http://controller.invalid:8000")
    assert [path for path, _ in proxy.seen] == [
        "http://controller.invalid:8000/mcp/tools/call",
        "http://controller.invalid:8000/mcp/tools",
        "http://controller.invalid:8000/mcp/tools/call",
    ]
    assert controller.seen == []
