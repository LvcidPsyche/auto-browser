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
import time
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
        answer = self.server.answer  # type: ignore[attr-defined]
        if answer:
            status, body = answer
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
    server.answer = None  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post_through_the_bridge(base_url: str, proxy: ThreadingHTTPServer) -> int:
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
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_BRIDGE_CHILD = """
import json, sys
from {module} import HttpMcpClient, StdioMcpBridge
client = HttpMcpClient(base_url=sys.argv[1], bearer_token=sys.argv[2], timeout_seconds=10)
bridge = StdioMcpBridge(client=client, stderr=sys.stderr)
print(json.dumps(bridge._handle_line(json.dumps({{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}}))))
"""


def _bridge_error_through_the_bridge(base_url: str, proxy: ThreadingHTTPServer) -> str:
    """The JSON-RPC error message the stdio bridge answers a tools/list with, the proxy named in its environment."""
    return _reply_through_the_bridge(base_url, proxy)["error"]["message"]


def _reply_through_the_bridge(base_url: str, proxy: ThreadingHTTPServer, proxy_url: str | None = None) -> dict:
    """What the stdio bridge answers a tools/list with, the proxy (or ``proxy_url``) named in its environment."""
    proxy_url = proxy_url or f"http://127.0.0.1:{proxy.server_port}"
    env = {"http_proxy": proxy_url, "HTTP_PROXY": proxy_url, "PYTHONPATH": str(_IMPORT_ROOT)}
    for name in ("PATH", "SYSTEMROOT"):  # SYSTEMROOT: Python's sockets need it on Windows
        if name in os.environ:
            env[name] = os.environ[name]
    result = subprocess.run(
        [sys.executable, "-c", _BRIDGE_CHILD.format(module=_MODULE), base_url, MARKER],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


UNREACHABLE = "No Auto Browser controller reachable"
INVALID = "is not a valid Auto Browser controller URL"


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
        reply = _reply_through_the_bridge(f"http://{host}:{controller.server_port}/mcp", recording_proxy)
    finally:
        controller.shutdown()
        controller.server_close()

    assert recording_proxy.seen == []  # type: ignore[attr-defined]
    # W10-16: the direct attempt was made: the controller answered it, or the bridge reported it unreachable.
    if "error" in reply:
        assert UNREACHABLE in reply["error"]["message"], reply
    else:
        assert controller.seen == [("/mcp", f"Bearer {MARKER}")]  # type: ignore[attr-defined]


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
        message = _bridge_error_through_the_bridge(f"http://{host}:{controller.server_port}/mcp", recording_proxy)
    finally:
        controller.shutdown()
        controller.server_close()

    assert recording_proxy.seen == []  # type: ignore[attr-defined]
    # W10-16: the refused direct connection, not a failure before any request, as W10-14's TypeError was.
    assert UNREACHABLE in message


@pytest.mark.parametrize("base_url", ["localhost:8000/mcp", "http:/127.0.0.1:9/mcp"])
def test_w10_14_a_host_less_base_url_reports_no_controller_reachable(recording_proxy, base_url: str) -> None:
    # urllib leaves request.host None for these (localhost: reads as a scheme), and the rule's "//" + None raised a
    # TypeError the bridge reported as an unexpected failure; urllib refuses them itself before any proxy is used.
    message = _bridge_error_through_the_bridge(base_url, recording_proxy)

    assert UNREACHABLE in message
    assert "Unexpected stdio bridge failure" not in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("host", ["::1", "%3a%3a1"])
def test_w11_1_an_unbracketed_ipv6_loopback_is_reached_directly_past_an_environment_proxy(
    recording_proxy, host: str
) -> None:
    # http.client reads the unbracketed ::1:PORT (and its percent-encoded spelling, which urllib decodes) as ::1,
    # while urlsplit read no host at all, so the token went to the proxy.
    try:
        controller = _serve("::1")
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


@pytest.mark.parametrize(
    "authority",
    [
        "::1",
        "%3a%3a1",
        "::ffff:127.0.0.1",
        # W12-1: the socket layer's idna codec maps these (a soft hyphen, a masculine ordinal) to localhost.
        "local%C2%ADhost",
        "l%C2%BAcalhost",
    ],
)
def test_w11_1_a_host_any_reading_finds_local_never_goes_through_the_environment_proxy(
    recording_proxy, authority: str
) -> None:
    # Each is local as the host http.client connects to and was, or could be, sent to the proxy by another reading.
    # The direct attempt may fail (nothing listens on ::1 at the controller's port, and a host without IPv6 has no
    # ::1 at all). W13-2 refuses the W11-1 authorities whose connect host is not local.
    controller = _serve()
    try:
        reply = _reply_through_the_bridge(f"http://{authority}:{controller.server_port}/mcp", recording_proxy)
    finally:
        controller.shutdown()
        controller.server_close()

    assert recording_proxy.seen == []  # type: ignore[attr-defined]
    if "error" in reply:
        assert UNREACHABLE in reply["error"]["message"], reply
    else:
        assert controller.seen == [("/mcp", f"Bearer {MARKER}")]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "base_url",
    [
        # W12-2: http.client splits the port at the last colon (host ":", or "::ffff:7f00" for IPv4-mapped
        # 127.0.0.1), and urlsplit reads no host; urllib's decoded host as a whole is local.
        "http://::1/mcp",
        "http://::/mcp",
        "http://%3a%3a1/mcp",
        "http://::ffff:7f00:1/mcp",
        # A userinfo hid a portless IPv6 loopback from every reading, so these went to the proxy.
        "http://user@::1/mcp",
        "http://user:pass@::1/mcp",
        "http://user@%3a%3a1/mcp",
        # W11-1: local to urllib's decoded host or the URL as written, but connected to as 127.0.0.2#.bad,
        # user:pass@127.0.0.1 and x/@127.0.0.1.
        "http://127.0.0.2%23.bad:{port}/mcp",
        "http://user:pass@127.0.0.1:{port}/mcp",
        "http://x%2f@127.0.0.1:{port}/mcp",
    ],
)
def test_w13_2_a_host_local_one_way_but_connected_to_as_another_is_refused(recording_proxy, base_url: str) -> None:
    # The direct attempt went to a name the resolver is asked for, and whoever answered got the token; a proxy
    # would be no better. Nothing is sent anywhere.
    controller = _serve()
    try:
        message = _bridge_error_through_the_bridge(base_url.format(port=controller.server_port), recording_proxy)
    finally:
        controller.shutdown()
        controller.server_close()

    assert INVALID in message
    assert "reads as a local host" in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]
    assert controller.seen == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("authority", ["%5b127.0.0.1%5d:{port}", "x%5b@127.0.0.1:{port}", "controller.invalid:abc"])
def test_w12_3_a_malformed_authority_is_refused_as_an_invalid_controller_url(recording_proxy, authority: str) -> None:
    # One reading's parse error (urlsplit's ValueError for the first two, http.client's InvalidURL for the port)
    # was reported as an unexpected bridge failure; it is a configuration error, and nothing is sent anywhere.
    controller = _serve()
    try:
        base_url = f"http://{authority.format(port=controller.server_port)}/mcp"
        message = _bridge_error_through_the_bridge(base_url, recording_proxy)
    finally:
        controller.shutdown()
        controller.server_close()

    assert INVALID in message
    # W13-1: the answer names the URL without its userinfo.
    assert base_url.replace("x%5b@", "") in message
    assert "Unexpected stdio bridge failure" not in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]
    assert controller.seen == []  # type: ignore[attr-defined]


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.parametrize(
    ("base_url", "host"),
    [
        ("http://alice:sentinel@controller.invalid/mcp", "controller.invalid"),
        ("http://alice:sentinel@controller.invalid:abc/mcp", "controller.invalid"),
        ("http://controller.invalid:abc/mcp?key=sentinel", "controller.invalid"),
        ("http://127.0.0.1:{port}/mcp?key=sentinel", "127.0.0.1"),
        # W14-1: the answers quoted urllib's and http.client's text, which quotes the URL or its decoded host.
        ("controller.invalid/mcp?key=sentinel#fragment-sentinel", "controller.invalid"),
        ("http://127.0.0.1:{port}/mcp?key=sentinel bad", "127.0.0.1"),
        ("http://alice:sentinel%20tail@controller.invalid/mcp", "controller.invalid"),
        ("http://alice:sentinel%27tail@controller.invalid/mcp", "controller.invalid"),
        ("http://alice:sentinel%2ftail@controller.invalid/mcp", "controller.invalid"),
        ("http://[::1/mcp?key=sentinel", "::1"),
        ("alice:sentinel@controller.invalid/mcp", "controller.invalid"),
    ],
)
def test_w13_1_an_answer_never_shows_the_urls_userinfo_or_query(recording_proxy, base_url: str, host: str) -> None:
    # The answer goes to the agent: it named the base URL whole, and http.client's InvalidURL text for a userinfo
    # without a port ("nonnumeric port: 'sentinel@controller.invalid'") carried the password.
    message = _bridge_error_through_the_bridge(base_url.format(port=_closed_port()), recording_proxy)

    assert "sentinel" not in message
    assert host in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "base_url",
    [
        "controller.invalid/mcp?key=sentinel#fragment-sentinel",
        "http://127.0.0.1:{port}/mcp?key=sentinel bad",
        "http://alice:sentinel%20tail@controller.invalid/mcp",
        "http://alice:sentinel%27tail@controller.invalid/mcp",
        "http://alice:sentinel%2ftail@controller.invalid/mcp",
        "http://[::1/mcp?key=sentinel",
    ],
)
def test_w14_1_a_url_the_bridge_cannot_read_is_refused_in_its_own_words(recording_proxy, base_url: str) -> None:
    # urllib's Request(), http.client's path check and its port parse each quoted the URL or its decoded host in
    # their text, which the answer showed; they are invalid controller URLs, and nothing is sent anywhere.
    controller = _serve()
    try:
        message = _bridge_error_through_the_bridge(base_url.format(port=controller.server_port), recording_proxy)
    finally:
        controller.shutdown()
        controller.server_close()

    assert INVALID in message
    assert "sentinel" not in message
    assert "Unexpected stdio bridge failure" not in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]
    assert controller.seen == []  # type: ignore[attr-defined]


def test_w14_1_a_proxys_error_detail_never_shows_the_urls_query(recording_proxy) -> None:
    # A proxy's JSON 502 whose detail echoes the URL reached the agent whole through the HTTP error message.
    recording_proxy.answer = (  # type: ignore[attr-defined]
        502,
        json.dumps({"detail": "upstream http://controller.invalid/mcp?key=sentinel failed"}).encode(),
    )

    message = _bridge_error_through_the_bridge("http://controller.invalid/mcp?key=sentinel", recording_proxy)

    assert "HTTP 502" in message
    assert "sentinel" not in message
    assert len(recording_proxy.seen) == 1  # type: ignore[attr-defined]


def test_w14_1_the_error_text_shown_drops_the_urls_known_secrets() -> None:
    from app.mcp_stdio import _redacted

    shown = _redacted(
        "nonnumeric port: 'sentinel tail@controller.invalid' for key=sentinel",
        "http://alice:sentinel%20tail@controller.invalid/mcp?key=sentinel",
    )

    assert shown == "nonnumeric port: 'controller.invalid' for [redacted]"


def test_w14_1_dropping_userinfo_runs_is_linear() -> None:
    from app.mcp_stdio import _without_userinfo

    started = time.monotonic()
    _without_userinfo("a" * 65536)

    assert time.monotonic() - started < 2


def test_w13_1_an_invalid_proxy_port_is_not_answered_as_an_invalid_controller_url(recording_proxy) -> None:
    # The base URL is valid; only the environment's proxy URL is not, so --base-url is not what to fix.
    reply = _reply_through_the_bridge(
        "http://controller.invalid/mcp", recording_proxy, proxy_url="http://proxy.invalid:abc"
    )
    message = reply["error"]["message"]

    assert INVALID not in message
    assert "Unexpected stdio bridge failure" in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]


def test_w13_1_the_error_text_shown_drops_every_userinfo() -> None:
    from app.mcp_stdio import _without_userinfo

    shown = _without_userinfo("nonnumeric port: 'sentinel@controller.invalid' via alice:sentinel@proxy.invalid:3128")

    assert shown == "nonnumeric port: 'controller.invalid' via proxy.invalid:3128"


def test_w11_1_a_non_numeric_port_is_refused_before_any_proxy_is_used(recording_proxy) -> None:
    # http.client's parse raises InvalidURL before an opener is chosen, so even a remote host never reaches the proxy.
    message = _bridge_error_through_the_bridge("http://controller.invalid:abc/mcp", recording_proxy)

    assert "http.client cannot read its host and port" in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]


def test_w11_1_a_loopback_userinfo_before_a_remote_host_still_goes_through_the_environment_proxy(
    recording_proxy,
) -> None:
    # 127.0.0.1 is the userinfo here; the host is controller.invalid all three ways, so the proxy is used.
    status = _post_through_the_bridge("http://127.0.0.1@controller.invalid/mcp", recording_proxy)

    assert status == 200
    assert [auth for _, auth in recording_proxy.seen] == [f"Bearer {MARKER}"]  # type: ignore[attr-defined]


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
