"""The controller's copy of the bridge (app.mcp_stdio) sends a loopback controller's token past any environment proxy.

urllib bypasses a proxy for localhost only when no_proxy names it, so with http_proxy set and no NO_PROXY the
bridge's Authorization header went to the proxy. A loopback host is now reached directly; any other host still
goes through the environment's proxy.

The bridge runs in a child process whose environment names the proxy from the start, as an operator's shell would:
urllib reads the proxy variables when an opener is built, which for the old module-level opener was at import.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

MARKER = "m" * 43
_IMPORT_ROOT = Path(__file__).resolve().parents[1]
_MODULE = "app.mcp_stdio"
_CHILD = """
import json, sys
from {module} import HttpMcpClient
client = HttpMcpClient(base_url=sys.argv[1], bearer_token=sys.argv[2], timeout_seconds=10)
response = client.post_json({{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}}, session_id=None)
print(json.dumps(response.status_code))
"""


class _Recording(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        # Read the body first: on Windows a socket closed with unread data resets.
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append((self.path, self.headers.get("Authorization")))  # type: ignore[attr-defined]
        redirect_to = self.server.redirect_to  # type: ignore[attr-defined]
        if redirect_to:
            # Once: a followed redirect comes back as a second request, which is answered and recorded.
            self.server.redirect_to = None  # type: ignore[attr-defined]
            self.send_response(302)
            self.send_header("Location", redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"jsonrpc": "2.0", "id": 1, "result": {}}')

    do_GET = do_POST  # noqa: N815 - a followed 302 turns the POST into a GET

    def log_message(self, *_: object) -> None:
        return


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _serve(host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = (_V6Server if ":" in host else ThreadingHTTPServer)((host, 0), _Recording)
    server.seen = []  # type: ignore[attr-defined]
    server.redirect_to = None  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post_through_the_bridge(base_url: str, proxy: ThreadingHTTPServer, *, check: bool = True) -> int | None:
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    env = {"http_proxy": proxy_url, "HTTP_PROXY": proxy_url, "PYTHONPATH": str(_IMPORT_ROOT)}
    for name in ("PATH", "SYSTEMROOT"):  # SYSTEMROOT: Python's sockets need it on Windows
        if name in os.environ:
            env[name] = os.environ[name]
    result = subprocess.run(
        [sys.executable, "-c", _CHILD.format(module=_MODULE), base_url, MARKER],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if not check:
        return None
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def recording_proxy():
    proxy = _serve()
    try:
        yield proxy
    finally:
        proxy.shutdown()
        proxy.server_close()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_a_loopback_controller_is_reached_directly_past_an_environment_proxy(recording_proxy, host: str) -> None:
    try:
        controller = _serve("::1" if host == "[::1]" else "127.0.0.1")
    except OSError:
        pytest.skip("no IPv6 loopback here")
    try:
        status = _post_through_the_bridge(f"http://{host}:{controller.server_port}/mcp", recording_proxy)
    finally:
        controller.shutdown()
        controller.server_close()

    assert status == 200
    assert recording_proxy.seen == []  # type: ignore[attr-defined]
    assert controller.seen == [("/mcp", f"Bearer {MARKER}")]  # type: ignore[attr-defined]


@pytest.mark.parametrize("host", ["0.0.0.0", "[::]", "localhost."])
def test_a_bind_all_or_trailing_dot_host_never_goes_through_the_environment_proxy(recording_proxy, host: str) -> None:
    # The direct request may fail where nothing answers on that address (0.0.0.0 on Windows, :: on a host without
    # IPv6); the assertion is what the proxy saw, which holds everywhere.
    controller = _serve()
    try:
        _post_through_the_bridge(f"http://{host}:{controller.server_port}/mcp", recording_proxy, check=False)
    finally:
        controller.shutdown()
        controller.server_close()

    assert recording_proxy.seen == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("host", ["127.2", "2130706434", "0x7f000002", "[::ffff:127.0.0.2]", "%31%32%37.0.0.2"])
def test_w9_5_a_loopback_address_written_another_way_never_goes_through_the_environment_proxy(
    recording_proxy, host: str
) -> None:
    # The socket layer connects to 127.0.0.2 for each (urllib decodes the percent-encoded host), while the bridge's
    # rule read them as names and sent the token to the proxy. Nothing listens on 127.0.0.2, so the direct request
    # fails; the assertion is what the proxy saw.
    if "%" not in host and not host.startswith("["):
        try:
            socket.getaddrinfo(host, 80, flags=socket.AI_NUMERICHOST)
        except OSError:
            pytest.skip(f"this platform's numeric parser does not read {host}, so no socket reaches it directly")
    controller = _serve()
    try:
        _post_through_the_bridge(f"http://{host}:{controller.server_port}/mcp", recording_proxy, check=False)
    finally:
        controller.shutdown()
        controller.server_close()

    assert recording_proxy.seen == []  # type: ignore[attr-defined]


def test_a_non_loopback_controller_still_goes_through_the_environment_proxy(recording_proxy) -> None:
    # controller.invalid is never resolved: the request goes to the (local) recording proxy.
    status = _post_through_the_bridge("http://controller.invalid/mcp", recording_proxy)

    assert status == 200
    assert recording_proxy.seen == [("http://controller.invalid/mcp", f"Bearer {MARKER}")]  # type: ignore[attr-defined]


def test_a_redirect_on_the_proxied_branch_is_not_followed(recording_proxy) -> None:
    # A followed redirect would carry the token to another host; the redirect tests use a loopback base URL, which
    # goes direct, so this is the one that covers the proxied opener (W8-4).
    recording_proxy.redirect_to = "http://elsewhere.invalid/mcp"  # type: ignore[attr-defined]

    status = _post_through_the_bridge("http://controller.invalid/mcp", recording_proxy)

    assert status == 302
    assert recording_proxy.seen == [("http://controller.invalid/mcp", f"Bearer {MARKER}")]  # type: ignore[attr-defined]
