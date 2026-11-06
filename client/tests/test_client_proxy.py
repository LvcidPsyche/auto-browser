"""AutoBrowserClient sends a loopback controller's token past any environment proxy, never to the proxy (W7-1).

httpx follows HTTP_PROXY, http_proxy and ALL_PROXY to a loopback host when NO_PROXY does not name it, so the
client's Authorization header for a local controller went to the proxy. A loopback base URL now gets a transport
of its own (httpx uses no environment proxy when one is given); any other host still goes through the proxy.
httpx reads the proxy variables when a client is built, so setting them in-process before the call is enough.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from auto_browser_client import AutoBrowserClient

MARKER = "m" * 43


class _Recording(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        self.server.seen.append((self.path, self.headers.get("Authorization")))  # type: ignore[attr-defined]
        body = json.dumps([]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def _serve() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recording)
    server.seen = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def servers(monkeypatch):
    proxy, controller = _serve(), _serve()
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    for name in ("NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("http_proxy", proxy_url)
    yield proxy, controller
    for server in (proxy, controller):
        server.shutdown()
        server.server_close()


def _loopback(controller: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{controller.server_port}"


def test_the_sync_client_reaches_a_loopback_controller_directly(servers) -> None:
    proxy, controller = servers
    with AutoBrowserClient(_loopback(controller), token=MARKER) as client:
        assert client.list_sessions() == []
    assert proxy.seen == []
    assert controller.seen == [("/sessions", f"Bearer {MARKER}")]


def test_the_lazily_opened_sync_client_reaches_a_loopback_controller_directly(servers) -> None:
    proxy, controller = servers
    client = AutoBrowserClient(_loopback(controller), token=MARKER)
    try:
        assert client.list_sessions() == []
    finally:
        client.close()
    assert proxy.seen == []
    assert controller.seen == [("/sessions", f"Bearer {MARKER}")]


def test_the_async_client_reaches_a_loopback_controller_directly(servers) -> None:
    proxy, controller = servers

    async def call() -> list:
        async with AutoBrowserClient(_loopback(controller), token=MARKER) as client:
            return await client.async_list_sessions()

    assert asyncio.run(call()) == []
    assert proxy.seen == []
    assert controller.seen == [("/sessions", f"Bearer {MARKER}")]


def test_a_remote_controller_still_goes_through_the_proxy(servers) -> None:
    proxy, controller = servers
    with AutoBrowserClient("http://controller.invalid:8000", token=MARKER) as client:
        assert client.list_sessions() == []

    async def call() -> list:
        async with AutoBrowserClient("http://controller.invalid:8000", token=MARKER) as client:
            return await client.async_list_sessions()

    assert asyncio.run(call()) == []
    assert [path for path, _ in proxy.seen] == ["http://controller.invalid:8000/sessions"] * 2
    assert controller.seen == []


@pytest.mark.parametrize("host", ["0.0.0.0", "[::]", "localhost."])
def test_a_bind_all_or_trailing_dot_host_never_goes_through_the_proxy(servers, host: str) -> None:
    # The direct request may fail where nothing answers on that address (0.0.0.0 on Windows, :: on a host without
    # IPv6); the assertion is what the proxy saw, which holds everywhere.
    proxy, controller = servers
    with AutoBrowserClient(f"http://{host}:{controller.server_port}", token=MARKER) as client:
        try:
            client.list_sessions()
        except Exception:
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

    async def call() -> None:
        async with AutoBrowserClient(f"http://{host}:{controller.server_port}", token=MARKER) as client:
            await client.async_list_sessions()

    with AutoBrowserClient(f"http://{host}:{controller.server_port}", token=MARKER) as client:
        try:
            client.list_sessions()
        except Exception:
            pass
    try:
        asyncio.run(call())
    except Exception:
        pass
    assert proxy.seen == []


def test_the_event_stream_reaches_a_loopback_controller_directly(servers) -> None:
    proxy, controller = servers
    with AutoBrowserClient(_loopback(controller), token=MARKER) as client:
        assert list(client.stream_events("s-1")) == []
    assert proxy.seen == []
    assert controller.seen == [("/sessions/s-1/events", f"Bearer {MARKER}")]


def test_the_event_stream_of_a_remote_controller_still_goes_through_the_proxy(servers) -> None:
    proxy, controller = servers
    with AutoBrowserClient("http://controller.invalid:8000", token=MARKER) as client:
        assert list(client.stream_events("s-1")) == []
    assert [path for path, _ in proxy.seen] == ["http://controller.invalid:8000/sessions/s-1/events"]
    assert controller.seen == []
