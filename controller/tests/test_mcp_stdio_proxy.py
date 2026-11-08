"""The controller's copy of the bridge (app.mcp_stdio) sends a loopback controller's token past any environment proxy.

urllib bypasses a proxy for localhost only when no_proxy names it, so with http_proxy set and no NO_PROXY the
bridge's Authorization header went to the proxy. A loopback host is now reached directly; any other host still
goes through the environment's proxy; and a host one reading finds local but that would be connected to as another
name, or that cannot be read, is refused with nothing sent.

The bridge runs in a child process whose environment names the proxy from the start, as an operator's shell would:
urllib reads the proxy variables when an opener is built, which for the old module-level opener was at import.
"""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request

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
            # (status, body), or (status, body, extra headers).
            status, body, *extra = answer
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (extra[0] if extra else {}).items():
                self.send_header(name, value)
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
def test_w10_14_a_host_less_base_url_is_refused_as_an_invalid_controller_url(recording_proxy, base_url: str) -> None:
    # urllib leaves request.host None for these (localhost: reads as a scheme), and the rule's "//" + None raised a
    # TypeError the bridge reported as an unexpected failure. They were then left to urllib's own error, answered
    # as no controller reachable; W15-3 refuses them as invalid before any opener is chosen.
    message = _bridge_error_through_the_bridge(base_url, recording_proxy)

    assert INVALID in message
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
        # W14-4: with a port as well, until the host http.client reads after the last @ was a reading.
        "http://user@::1:8000/mcp",
        "http://user:pass@::1:8000/mcp",
        "http://user@%3a%3a1:8000/mcp",
        "http://user@::ffff:127.0.0.1:8000/mcp",
        # W11-1: local to urllib's decoded host or the URL as written, but connected to as 127.0.0.2#.bad,
        # user:pass@127.0.0.1 and x/@127.0.0.1.
        "http://127.0.0.2%23.bad:{port}/mcp",
        "http://user:pass@127.0.0.1:{port}/mcp",
        "http://x%2f@127.0.0.1:{port}/mcp",
        # W15-1: only the URL as written reads a local host (urlsplit's hostname stops at the first colon after the
        # last literal @); every decoded reading finds controller.invalid, and the connect host is
        # x@127.0.0.1:@controller.invalid.
        "http://x@127.0.0.1:%40controller.invalid:80/mcp",
        "http://x@localhost:%40controller.invalid:80/mcp",
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


def _urlsplit_refuses_a_bracketed_ipv4() -> bool:
    """Whether urlsplit raises ValueError for a bracketed IPv4 host, as it does from Python 3.11.4 (W14-2)."""
    try:
        urlsplit("//[127.0.0.1]")
    except ValueError:
        return True
    return False


@pytest.mark.parametrize(
    "authority",
    [
        pytest.param(
            "%5b127.0.0.1%5d:{port}",
            marks=pytest.mark.skipif(
                not _urlsplit_refuses_a_bracketed_ipv4(),
                reason="this Python's urlsplit does not refuse a bracketed IPv4 host (it does from 3.11.4)",
            ),
        ),
        "x%5b@127.0.0.1:{port}",
        "controller.invalid:abc",
    ],
)
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
    # W15-2: the answer names no part of the URL: not the URL, nor its host or userinfo as written. 127.0.0.1 is not
    # checked: the W13-2 reason names http://127.0.0.1:PORT as an example.
    userinfo, _, host_and_port = authority.rpartition("@")
    named = [part for part in (base_url, userinfo, host_and_port.rpartition(":")[0]) if part and part != "127.0.0.1"]
    assert [part for part in named if part in message] == []
    assert "Unexpected stdio bridge failure" not in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]
    assert controller.seen == []  # type: ignore[attr-defined]


@pytest.fixture
def closed_port():
    """A port held bound but not listening until the test ends, so no other process takes it and a connect is
    refused (W14-3)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        yield probe.getsockname()[1]


# How often the echo server's accept() and reads wake to see whether its test is over, and how long it reads a
# connection it accepted (R5C-31).
_ECHO_POLL_SECONDS = 0.1
_ECHO_READ_SECONDS = 10.0


def _echo_request_line(connection: socket.socket, stop: threading.Event) -> None:
    """Answer one request with its own request line as the status line, once its headers and body are read.

    The reads end when the test is over or after _ECHO_READ_SECONDS, however the client sends: a read's timeout alone
    bounded only a client gone quiet, so one sending a header line now and then kept the worker reading after its
    test (review of c3)."""
    deadline = time.monotonic() + _ECHO_READ_SECONDS
    connection.settimeout(_ECHO_POLL_SECONDS)
    received = b""
    while True:
        head, ended, body = received.partition(b"\r\n\r\n")
        # The body is read too: on Windows a socket closed with unread data resets.
        if ended and len(body) >= _content_length(head):
            break
        if stop.is_set() or time.monotonic() > deadline:
            return
        try:
            chunk = connection.recv(65536)
        except TimeoutError:
            continue
        if not chunk:
            return
        received += chunk
    connection.sendall(head.split(b"\r\n", 1)[0] + b"\r\nContent-Length: 0\r\n\r\n")


def _content_length(head: bytes) -> int:
    for header in head.split(b"\r\n")[1:]:
        name, _, value = header.partition(b":")
        if name.strip().lower() == b"content-length":
            return int(value)
    return 0


@pytest.fixture
def request_line_echo():
    """A raw TCP server on 127.0.0.1 that answers each request with its own request line as the status line.

    http.client then raises BadStatusLine quoting that line, the URL's query included, which is not a URLError, so
    the bridge answers with its unexpected-failure message (W15-1). Its accept() and its reads wake to see whether the
    test is over, a connection's reads are bounded, and its worker is stopped and joined when the test ends (R5C-31):
    closing the listener from another thread does not reliably wake a thread blocked in accept(), and a connection's
    reads had no bound."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(_ECHO_POLL_SECONDS)
    stop = threading.Event()

    def answer() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                try:
                    _echo_request_line(connection, stop)
                except OSError:  # a connection reset
                    continue

    worker = threading.Thread(target=answer, daemon=True)
    worker.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        worker.join(_ECHO_READ_SECONDS + _ECHO_POLL_SECONDS + 5)
        listener.close()
        assert not worker.is_alive(), "the request line echo server's worker did not stop"


def test_r5c_31_the_echo_servers_worker_stops_while_a_client_is_still_sending(request_line_echo: int) -> None:
    # Review of c3 (Codex 8): a read timed out only when the client went quiet, so a client sending a header line
    # every 100 ms kept the worker reading after its test, and the teardown failed 15 s later with the worker alive.
    # The teardown is the assertion: it joins the worker and fails while the worker lives.
    client = socket.create_connection(("127.0.0.1", request_line_echo))

    def drip() -> None:
        deadline = time.monotonic() + 30
        with client:
            try:
                client.sendall(b"POST /mcp HTTP/1.1\r\n")
                while time.monotonic() < deadline:
                    client.sendall(b"X-Drip: 1\r\n")
                    time.sleep(0.1)
            except OSError:  # the server closed the connection: its worker stopped reading
                return

    threading.Thread(target=drip, daemon=True).start()
    time.sleep(0.3)  # the worker is reading the drip's header lines when the test ends


# Of test_w13_1's base URLs, the one answered with urllib's URLError, the answer that no controller is reachable;
# each other one is an invalid controller URL (alice:sentinel@... too since W15-3: its scheme reads as "alice").
_ANSWERED_AS_UNREACHABLE = ("http://127.0.0.1:{port}/mcp?key=sentinel",)


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
        # W15-2: an unencoded # in the password, which the URL cut as a string ended at, so it named the password.
        ("http://alice:sentinel#tail@controller.invalid/mcp", "controller.invalid"),
    ],
)
def test_w13_1_an_answer_never_shows_the_urls_userinfo_or_query(
    recording_proxy, closed_port: int, base_url: str, host: str
) -> None:
    # The answer goes to the agent: it named the base URL whole, and http.client's InvalidURL text for a userinfo
    # without a port ("nonnumeric port: 'sentinel@controller.invalid'") carried the password. The port stays bound,
    # not listening, until the answer is read (W14-3).
    message = _bridge_error_through_the_bridge(base_url.format(port=closed_port), recording_proxy)

    assert "sentinel" not in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]
    if base_url in _ANSWERED_AS_UNREACHABLE:
        # The answer that no controller is reachable names the URL without its userinfo, query or fragment.
        assert UNREACHABLE in message
        assert host in message
    else:
        # W15-2: the invalid answer names no part of the URL: not the URL, its host, its user name or its query.
        # 127.0.0.1 is not checked: the W13-2 reason names http://127.0.0.1:PORT as an example.
        assert INVALID in message
        named = [base_url.format(port=closed_port), host, "alice", "key="]
        assert [part for part in named if part != "127.0.0.1" and part in message] == []


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


_A_RESULT = '{"jsonrpc":"2.0","id":1,"result":{"from":"sentinel"}}'


@pytest.mark.parametrize(
    "base_url",
    [
        # urllib reads a scheme-less userinfo's user name as the scheme, and the answer quoted it.
        "sentinel:x@controller.invalid/mcp",
        # Codex Q02: answered as no controller reachable "at alice:sentinel controller.invalid/mcp".
        "alice:sentinel tail@controller.invalid/mcp?next=//here",
        "localhost:8000/mcp",
        "http:///mcp",
        # urllib's default handlers answered these from the URL or the local file, as if a controller had.
        "data:application/json," + _A_RESULT,
        "FILE_URI",
    ],
)
def test_w15_3_a_base_url_that_is_not_http_or_https_with_a_host_is_refused(
    recording_proxy, tmp_path: Path, base_url: str
) -> None:
    result_file = tmp_path / "result.json"
    result_file.write_text(_A_RESULT, encoding="utf-8")

    reply = _reply_through_the_bridge(base_url.replace("FILE_URI", result_file.as_uri()), recording_proxy)

    assert "result" not in reply, reply
    assert INVALID in reply["error"]["message"]
    assert "it is not an http:// or https:// URL with a host" in reply["error"]["message"]
    assert "sentinel" not in json.dumps(reply)
    assert "alice" not in json.dumps(reply)
    assert recording_proxy.seen == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:99999/mcp",
        "http://127.0.0.1:65536/mcp",
        "http://127.0.0.1:-1/mcp",
        "http://[::1]:99999/mcp",
        "http://controller.invalid:99999/mcp",
    ],
)
def test_w15_4_a_port_outside_0_to_65535_is_refused(recording_proxy, base_url: str) -> None:
    # http.client reads these ports as written and the socket layer wraps them: on Windows getaddrinfo connects
    # 99999 to 34463, 65536 to 0 and -1 to 65535, so the token went to another local port than the one written.
    message = _bridge_error_through_the_bridge(base_url, recording_proxy)

    assert INVALID in message
    assert "its port is outside 0 to 65535" in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "base_url",
    [
        # Request() refuses both though each has a scheme and balanced brackets: the netloc holds a character that NFKC
        # normalizes to a colon, and notanip is not an IP address. The reason said "(no scheme, or an unbalanced
        # bracket)".
        "http://controller.invalid" + chr(0xFF1A) + "8000/mcp",
        "http://[notanip]:8000/mcp",
        # A reading's parse error: urlsplit refuses the decoded x[@127.0.0.1:8000.
        "http://x%5b@127.0.0.1:8000/mcp",
    ],
)
def test_w15_5_a_fixed_reason_gives_its_causes_as_examples(recording_proxy, base_url: str) -> None:
    message = _bridge_error_through_the_bridge(base_url, recording_proxy)

    assert INVALID in message
    assert "(for example, " in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("base_url", "reason"),
    [
        # http.client encodes the request line as ASCII, after the opener is chosen: "Unexpected stdio bridge failure:
        # 'ascii' codec can't encode character ...".
        ("http://127.0.0.1:{port}/mcp/" + chr(0xE9), "or a character outside ASCII; percent-encode it"),
        ("http://controller.invalid/mcp?key=sentinel" + chr(0xE9), "or a character outside ASCII; percent-encode it"),
        # urllib puts the fragment into a proxied request line, which http.client refuses for a space or a control
        # character ("URL can't contain control characters") and cannot encode outside ASCII.
        ("http://controller.invalid/mcp?key=sentinel#frag sentinel", "its fragment holds"),
        ("http://controller.invalid/mcp#frag" + chr(0x7F) + "sentinel", "its fragment holds"),
        ("http://controller.invalid/mcp#frag" + chr(0xE9) + "sentinel", "its fragment holds"),
    ],
    ids=["path_non_ascii", "query_non_ascii", "fragment_space", "fragment_control", "fragment_non_ascii"],
)
def test_r5c_28_a_url_http_client_cannot_send_is_refused_as_an_invalid_controller_url(
    recording_proxy, base_url: str, reason: str
) -> None:
    controller = _serve()
    try:
        message = _bridge_error_through_the_bridge(base_url.format(port=controller.server_port), recording_proxy)
    finally:
        controller.shutdown()
        controller.server_close()

    assert INVALID in message
    assert reason in message
    assert "Unexpected stdio bridge failure" not in message
    assert "sentinel" not in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]
    assert controller.seen == []  # type: ignore[attr-defined]


_HOST_OR_USERINFO = "its host or userinfo holds a character http.client cannot send"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://contr" + chr(0xF4) + "leur.invalid/mcp?key=sentinel",
        "http://sentinel" + chr(0x43A) + "@controller.invalid/mcp",
    ],
    ids=["host_outside_ascii", "userinfo_outside_latin_1"],
)
def test_r5c_28_a_host_or_userinfo_a_proxy_cannot_get_is_refused_as_an_invalid_controller_url(
    recording_proxy, base_url: str
) -> None:
    # Review of c3 (Codex 7): the path, query and fragment were checked, but urllib puts the URL whole, host and
    # userinfo too, into the request line it sends an environment proxy, which http.client encodes as ASCII, and the
    # answer was "Unexpected stdio bridge failure: 'ascii' codec can't encode character ... in position 12", one
    # character of the URL and where it is.
    message = _bridge_error_through_the_bridge(base_url, recording_proxy)

    assert INVALID in message
    assert _HOST_OR_USERINFO in message
    assert "Unexpected stdio bridge failure" not in message
    assert "sentinel" not in message
    assert "position" not in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]


def test_r5c_28_a_userinfo_the_host_header_cannot_carry_is_refused_without_a_proxy(monkeypatch) -> None:
    # Review of c3: without a proxy urllib sends the host it decoded, userinfo included, as the Host header, which
    # http.client encodes as latin-1; a percent-encoded character outside it failed as above, naming that character.
    import app.mcp_stdio as bridge

    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "*")  # and no proxy from the Windows registry either
    client = bridge.HttpMcpClient(base_url="http://sentinel%D0%BA@controller.invalid/mcp")
    reply = bridge.StdioMcpBridge(client=client, stderr=io.StringIO())._handle_line(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    )
    assert reply is not None
    message = reply["error"]["message"]

    assert INVALID in message
    assert _HOST_OR_USERINFO in message
    assert "sentinel" not in message
    assert "position" not in message


@pytest.mark.parametrize("scheme", ["file", "data", "ftp"])
def test_r5c_28_the_bridges_openers_answer_no_file_data_or_ftp_url(tmp_path: Path, scheme: str) -> None:
    # urlopen refuses such a URL before any opener is chosen (W15-3 refused it in HttpMcpClient only); the openers
    # themselves kept urllib's default handlers, which answer a file: or data: URL from the local file or the URL
    # itself, as if a controller had, and an ftp: one from an FTP server (here none listens, so urllib's FTP handler
    # failed with its own error).
    import app.mcp_stdio as bridge

    result_file = tmp_path / "result.json"
    result_file.write_text(_A_RESULT, encoding="utf-8")
    url = {
        "file": "file://localhost" + result_file.as_uri().removeprefix("file://"),
        "data": "data:application/json," + _A_RESULT,
        "ftp": "ftp://127.0.0.1:9/result.json",
    }[scheme]

    with pytest.raises(bridge.InvalidControllerURL, match="it is not an http:// or https:// URL with a host"):
        bridge.urlopen(Request(url), timeout=5)
    for opener in (bridge._DIRECT, bridge._opener()):
        with pytest.raises(URLError, match=f"unknown url type: {scheme}"):
            opener.open(Request(url), timeout=5)


def test_r5c_28_a_url_of_a_scheme_an_environment_proxy_names_never_goes_to_that_proxy(
    recording_proxy, monkeypatch
) -> None:
    # Found by the orchestrator (c4): with ftp_proxy set, urllib's ProxyHandler turned an ftp:// request into an http
    # one to that proxy before UnknownHandler was reached, so the proxied opener sent it there, bearer token included,
    # and urlopen returned the proxy's answer as the controller's.
    import app.mcp_stdio as bridge

    monkeypatch.setenv("ftp_proxy", f"http://127.0.0.1:{recording_proxy.server_port}")

    def request() -> Request:  # a fresh one each time: a proxied open rewrites the request it is given
        return Request("ftp://controller.example/mcp", headers={"Authorization": f"Bearer {MARKER}"})

    with pytest.raises(bridge.InvalidControllerURL, match="it is not an http:// or https:// URL with a host"):
        bridge.urlopen(request(), timeout=5)
    # The proxied opener takes only the environment's http and https proxies.
    with pytest.raises(URLError, match="unknown url type: ftp"):
        bridge._opener().open(request(), timeout=5)
    assert recording_proxy.seen == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("retry_after", "shown"),
    [
        # Only the detail went through _shown: a Retry-After quoting the URL was shown whole.
        ("http://controller.invalid/mcp?key=sentinel", " (rate limited)"),
        ("Wed, 21 Oct 2026 07:28:00 GMT", " (rate limited)"),
        (" 12 ", " (rate limited; retry after 12s)"),
    ],
)
def test_w15_6_a_429s_retry_after_is_shown_only_as_seconds(recording_proxy, retry_after: str, shown: str) -> None:
    recording_proxy.answer = (  # type: ignore[attr-defined]
        429,
        json.dumps({"detail": "Too many requests"}).encode(),
        {"Retry-After": retry_after},
    )

    message = _bridge_error_through_the_bridge("http://controller.invalid/mcp?key=sentinel", recording_proxy)

    assert message == f"Auto Browser controller returned HTTP 429: Too many requests{shown}"
    assert len(recording_proxy.seen) == 1  # type: ignore[attr-defined]


def test_w15_6_the_error_text_shown_drops_the_urls_secrets_when_the_url_ends_in_a_cr() -> None:
    # A CRLF .env file leaves a CR on the URL; urllib's Request strips it, so the values to drop must be read the same
    # way, or key=sentinel + CR matches no text.
    from app.mcp_stdio import _redacted

    assert _redacted("upstream key=sentinel failed", "http://controller.invalid/mcp?key=sentinel" + chr(13)) == (
        "upstream [redacted] failed"
    )


def test_w15_6_the_unreachable_answer_names_the_url_as_urllib_reads_it(closed_port: int) -> None:
    # urllib's Request strips surrounding whitespace and a <URL:...> wrapper; the answer named the URL as given.
    import app.mcp_stdio as bridge

    client = bridge.HttpMcpClient(base_url=f" <URL:http://127.0.0.1:{closed_port}/mcp?key=sentinel>\r")
    reply = bridge.StdioMcpBridge(client=client, stderr=io.StringIO())._handle_line(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    )
    assert reply is not None
    message = reply["error"]["message"]

    assert f"{UNREACHABLE} at http://127.0.0.1:{closed_port}/mcp — " in message
    assert "sentinel" not in message


def test_w15_6_the_token_file_problem_is_shown_without_the_urls_secrets(tmp_path: Path) -> None:
    # The 401 answer adds why no token was sent, which names the token file; that text goes through _shown, like the
    # detail. Here the file's path holds the base URL's query.
    import app.mcp_stdio as bridge

    controller = _serve()
    controller.answer = (401, b'{"detail": "Missing or invalid bearer token"}')  # type: ignore[attr-defined]
    try:
        client = bridge.HttpMcpClient(
            base_url=f"http://127.0.0.1:{controller.server_port}/mcp?key=sentinel",
            token_file=str(tmp_path / "key=sentinel" / "api-token"),
        )
        reply = bridge.StdioMcpBridge(client=client, stderr=io.StringIO())._handle_line(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        )
    finally:
        controller.shutdown()
        controller.server_close()
    assert reply is not None
    message = reply["error"]["message"]

    assert "HTTP 401" in message
    assert "could not read the token file" in message
    assert "sentinel" not in message


@pytest.mark.parametrize(
    ("token", "in_file"),
    [
        ("sentinelA\r\nsentinelB", False),
        # http.client lets a line break before a space or tab through, as an obsolete header fold, and sent it.
        ("sentinelA\n sentinelB", False),
        ("sentinelA\nsentinelB", True),
    ],
    ids=["bearer_token_crlf", "bearer_token_folded", "token_file_lf"],
)
def test_w15_7_a_token_holding_a_line_break_is_never_sent(tmp_path: Path, token: str, in_file: bool) -> None:
    # http.client refused the header with a ValueError that quoted it whole, and the generic answer showed the
    # controller's token to the agent.
    import app.mcp_stdio as bridge

    token_file = tmp_path / "api-token"
    token_file.write_bytes(token.encode() + b"\n")
    controller = _serve()
    controller.answer = (401, b'{"detail": "Missing or invalid bearer token"}')  # type: ignore[attr-defined]
    try:
        client = bridge.HttpMcpClient(
            base_url=f"http://127.0.0.1:{controller.server_port}/mcp",
            bearer_token=None if in_file else token,
            token_file=str(token_file) if in_file else None,
        )
        reply = bridge.StdioMcpBridge(client=client, stderr=io.StringIO())._handle_line(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        )
    finally:
        controller.shutdown()
        controller.server_close()
    assert reply is not None
    message = reply["error"]["message"]

    assert "HTTP 401" in message
    assert "holds a line break, which an HTTP header cannot carry, so no token was sent" in message
    assert "sentinel" not in message
    assert controller.seen == [("/mcp", None)]  # type: ignore[attr-defined]


def _answer_with_a_401(detail: str = "Missing or invalid bearer token", **client_options: object) -> tuple[dict, list]:
    """The bridge's answer to a tools/list a controller answers 401 with ``detail``, and what that controller saw."""
    import app.mcp_stdio as bridge

    controller = _serve()
    controller.answer = (401, json.dumps({"detail": detail}).encode())  # type: ignore[attr-defined]
    try:
        client = bridge.HttpMcpClient(base_url=f"http://127.0.0.1:{controller.server_port}/mcp", **client_options)
        reply = bridge.StdioMcpBridge(client=client, stderr=io.StringIO())._handle_line(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        )
    finally:
        controller.shutdown()
        controller.server_close()
    assert reply is not None
    return reply, controller.seen  # type: ignore[attr-defined]


@pytest.mark.parametrize("in_file", [False, True], ids=["bearer_token", "token_file"])
def test_r5c_30_a_token_holding_a_character_outside_latin_1_is_never_sent(tmp_path: Path, in_file: bool) -> None:
    # http.client encodes a header as latin-1, and its UnicodeEncodeError named the character and its position in the
    # header, which the generic answer showed the agent: one character of the controller's token, and where it is.
    token = "sentinelA" + chr(0x20AC) + "sentinelB"
    token_file = tmp_path / "api-token"
    token_file.write_bytes(token.encode("utf-8") + b"\n")

    reply, seen = _answer_with_a_401(
        bearer_token=None if in_file else token, token_file=str(token_file) if in_file else None
    )
    message = reply["error"]["message"]

    assert "HTTP 401" in message
    assert "holds a character outside latin-1, which an HTTP header cannot carry, so no token was sent" in message
    assert ("the token file" in message) is in_file
    assert "sentinel" not in message
    assert chr(0x20AC) not in message
    assert seen == [("/mcp", None)]


def test_r5c_30_a_token_inside_latin_1_is_still_sent() -> None:
    # The refusal is what http.client cannot encode, not every non-ASCII token: the controller reads the header as
    # latin-1 and compares it as UTF-8 (auth_policy.match_credential), so such a token authenticates today.
    token = "sentinel" + chr(0xE4)

    reply, seen = _answer_with_a_401(bearer_token=token)

    assert "no token was sent" not in reply["error"]["message"]
    assert seen == [("/mcp", f"Bearer {token}")]


def test_r5c_30_a_token_file_that_is_not_utf_8_is_answered_without_its_bytes(tmp_path: Path) -> None:
    # Review of c3 (Codex 6): the 401's answer quoted the decode error, which names one of the token's bytes and its
    # position ("can't decode byte 0x80 in position 9"), as R5C-30's encode error named a character.
    token_file = tmp_path / "api-token"
    token_file.write_bytes(b"sentinelA" + bytes([0x80]) + b"sentinelB\n")

    reply, seen = _answer_with_a_401(token_file=str(token_file))
    message = reply["error"]["message"]

    assert "HTTP 401" in message
    assert f"the token file {token_file} is not UTF-8 text, so no token was sent" in message
    assert "0x80" not in message
    assert "position" not in message
    assert seen == [("/mcp", None)]


@pytest.mark.parametrize(
    ("base_url", "said"),
    [
        ("http://127.0.0.1:{port}/mcp?key=sentinel", UNREACHABLE),
        ("http://controller.invalid:abc/mcp?key=sentinel", INVALID),
    ],
    ids=["unreachable", "invalid"],
)
def test_r5c_23_a_notification_gets_no_answer_when_the_controller_cannot_be_reached(
    closed_port: int, base_url: str, said: str
) -> None:
    # A notification (no id) must get no answer; the URLError and invalid-URL paths answered one with "id": null,
    # which matches no request the client sent. The diagnostic goes to stderr, without the URL's secrets.
    import app.mcp_stdio as bridge

    stderr = io.StringIO()
    client = bridge.HttpMcpClient(base_url=base_url.format(port=closed_port))
    reply = bridge.StdioMcpBridge(client=client, stderr=stderr)._handle_line(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    )

    assert reply is None
    assert said in stderr.getvalue()
    assert "sentinel" not in stderr.getvalue()


@pytest.mark.parametrize(
    ("status", "said"),
    [
        (401, "Auto Browser controller returned HTTP 401: refused [redacted]"),
        (503, "Empty or non-JSON response from Auto Browser MCP endpoint (503)"),
        (202, None),
    ],
)
def test_r5c_23_a_notification_the_controller_answers_with_an_http_error_is_reported_on_stderr(
    status: int, said: str | None
) -> None:
    # c3's "not done": a notification the controller answered with an HTTP error (a 401 for a bad token, a 5xx) was
    # dropped without a word, where one the controller never got is reported on stderr (R5C-23). It still gets no
    # answer; one the controller accepted (202) prints nothing.
    import app.mcp_stdio as bridge

    controller = _serve()
    body = json.dumps({"detail": "refused sentinel-0123"}).encode() if status == 401 else b""
    controller.answer = (status, body)  # type: ignore[attr-defined]
    stderr = io.StringIO()
    try:
        client = bridge.HttpMcpClient(base_url=f"http://127.0.0.1:{controller.server_port}/mcp?key=sentinel-0123")
        reply = bridge.StdioMcpBridge(client=client, stderr=stderr)._handle_line(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )
    finally:
        controller.shutdown()
        controller.server_close()

    assert reply is None
    if said is None:
        assert stderr.getvalue() == ""
    else:
        assert stderr.getvalue().startswith(f"stdio bridge: {said}")
    assert "sentinel" not in stderr.getvalue()


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


_BACKSLASH = chr(92)


@pytest.mark.parametrize(
    ("base_url", "detail"),
    [
        # Codex Q01: a query value quoted without its key matched no part of the URL, which was matched whole.
        ("http://controller.invalid/mcp?key=sentinel-0123&page=2", "bad key sentinel-0123"),
        # A value quoted as repr() or json.dumps() writes it: str() of a structure in a server's own text.
        (
            "http://controller.invalid/mcp?key=sentinel%5Ctail",
            "rejected " + repr({"key": "sentinel" + _BACKSLASH + "tail"}),
        ),
        ("http://controller.invalid/mcp?key=sen%22tinel-0123", "rejected " + json.dumps({"key": 'sen"tinel-0123'})),
    ],
    ids=["query_value_alone", "repr_form", "json_form"],
)
def test_r5c_20_a_servers_detail_never_shows_a_secret_of_the_url_quoted_another_way(
    recording_proxy, base_url: str, detail: str
) -> None:
    recording_proxy.answer = (502, json.dumps({"detail": detail}).encode())  # type: ignore[attr-defined]

    message = _bridge_error_through_the_bridge(base_url, recording_proxy)

    assert message.startswith("Auto Browser controller returned HTTP 502: ")
    assert "[redacted]" in message
    assert "tinel" not in message


@pytest.mark.parametrize(
    ("base_url", "detail", "shown"),
    [
        # FastAPI's 422 detail quotes the value alone, in a list of objects.
        (
            "http://controller.invalid/mcp?key=sentinel-0123",
            [{"loc": ["query", "key"], "input": "sentinel-0123", "ctx": {"n": 7}}],
            [{"loc": ["query", "key"], "input": "[redacted]", "ctx": {"n": 7}}],
        ),
        # str() escaped the backslash a decoded query holds, so the decoded value matched nothing.
        (
            "http://controller.invalid/mcp?key=sentinel%5Ctail",
            {"upstream": "key=sentinel" + _BACKSLASH + "tail"},
            {"upstream": "[redacted]"},
        ),
        # A number that is a secret of the URL, as Codex R03's Retry-After was.
        (
            "http://alice:93847561029384756102@controller.invalid:8000/mcp",
            {"retry": 93847561029384756102},
            {"retry": "[redacted]"},
        ),
    ],
    ids=["fastapi_422", "decoded_backslash", "numeric_secret"],
)
def test_r5c_20_a_structured_detail_is_shown_as_json_without_the_urls_secrets(
    recording_proxy, base_url: str, detail: object, shown: object
) -> None:
    recording_proxy.answer = (502, json.dumps({"detail": detail}).encode())  # type: ignore[attr-defined]
    prefix = "Auto Browser controller returned HTTP 502: "

    message = _bridge_error_through_the_bridge(base_url, recording_proxy)

    assert message.startswith(prefix)
    assert json.loads(message[len(prefix) :]) == shown


@pytest.mark.parametrize("in_file", [False, True], ids=["bearer_token", "token_file"])
def test_r5c_20_a_servers_detail_never_shows_the_token(tmp_path: Path, in_file: bool) -> None:
    # Codex R02: the answers dropped the URL's secrets but not the bearer token the request carried.
    token_file = tmp_path / "api-token"
    token_file.write_text(MARKER + "\n", encoding="utf-8")

    reply, seen = _answer_with_a_401(
        f"token {MARKER} is not valid",
        bearer_token=None if in_file else MARKER,
        token_file=str(token_file) if in_file else None,
    )

    assert reply["error"]["message"].startswith("Auto Browser controller returned HTTP 401: token [redacted] is not")
    assert MARKER not in reply["error"]["message"]
    assert seen == [("/mcp", f"Bearer {MARKER}")]


def test_r5c_20_a_forwarded_json_rpc_errors_message_is_shown_without_secrets_and_a_result_is_untouched(
    recording_proxy,
) -> None:
    # Codex P03: the bridges forwarded a controller's JSON-RPC body unchanged, so an error message quoting the URL (a
    # proxy, a gateway or the controller echoing it) reached the agent. Only an error's message is shown that way.
    base_url = "http://controller.invalid/mcp?key=sentinel-0123"
    message = f"upstream {base_url} refused {MARKER}"
    error = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": message, "data": {"kept": "as sent"}}}
    recording_proxy.answer = (200, json.dumps(error).encode())  # type: ignore[attr-defined]

    reply = _reply_through_the_bridge(base_url, recording_proxy)

    assert reply["error"] == {
        "code": -32001,
        "message": "upstream http://controller.invalid/mcp?[redacted] refused [redacted]",
        "data": {"kept": "as sent"},
    }
    result = {"jsonrpc": "2.0", "id": 1, "result": {"text": message}}
    recording_proxy.answer = (200, json.dumps(result).encode())  # type: ignore[attr-defined]
    assert _reply_through_the_bridge(base_url, recording_proxy) == result


def test_r5c_20_a_forwarded_json_rpc_errors_data_is_shown_without_secrets(recording_proxy) -> None:
    # c4: only the message was redacted, and a proxy or gateway can quote the URL or the token in the data too, which
    # an agent reads as well. Its shape is kept, a number that is not a secret too.
    base_url = "http://controller.invalid/mcp?key=sentinel-0123"
    data = {"upstream": base_url, "auth": f"Bearer {MARKER}", "tried": ["sentinel-0123", 2], "n": 7}
    error = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "upstream refused", "data": data}}
    recording_proxy.answer = (200, json.dumps(error).encode())  # type: ignore[attr-defined]

    reply = _reply_through_the_bridge(base_url, recording_proxy)

    assert reply["error"] == {
        "code": -32001,
        "message": "upstream refused",
        "data": {
            "upstream": "http://controller.invalid/mcp?[redacted]",
            "auth": "Bearer [redacted]",
            "tried": ["[redacted]", 2],
            "n": 7,
        },
    }


def test_c4_a_forwarded_error_whose_data_nests_deeper_than_a_walk_can_go_is_still_answered(recording_proxy) -> None:
    # c4 review: the data's redaction walk had no depth limit, and its RecursionError ended the bridge with no answer.
    # Past the walk's depth (_REDACTED_DEPTH_MAX, 100) the value is replaced, not shown. This depth is just over it,
    # and one every supported Python (3.11 to 3.14) parses: 3.11's json.loads fails near 1,000 (CI's host test).
    depth = 105
    data = "[" * depth + "]" * depth
    error = '{"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "deep", "data": ' + data + "}}"
    recording_proxy.answer = (200, error.encode())  # type: ignore[attr-defined]

    reply = _reply_through_the_bridge("http://controller.invalid/mcp", recording_proxy)

    assert (reply["error"]["code"], reply["error"]["message"]) == (-32001, "deep")
    assert "[redacted: nested too deep]" in json.dumps(reply)


def test_a_body_too_deep_for_any_python_to_parse_is_answered_as_an_unreadable_one(recording_proxy) -> None:
    # json.loads raises RecursionError on this on every supported Python (3.11 to 3.14), as it raises ValueError on a
    # proxy's HTML page: the bridge reports the status of a body it cannot read, not an unexpected failure.
    depth = 1_000_000
    body = '{"jsonrpc": "2.0", "id": 1, "result": ' + "[" * depth + "]" * depth + "}"
    recording_proxy.answer = (200, body.encode())  # type: ignore[attr-defined]

    reply = _reply_through_the_bridge("http://controller.invalid/mcp", recording_proxy)

    assert reply["error"]["code"] == -32000
    assert reply["error"]["message"] == "Empty or non-JSON response from Auto Browser MCP endpoint (200)"


def test_an_answer_nested_too_deep_to_encode_is_answered_as_an_error_and_the_bridge_goes_on(monkeypatch) -> None:
    # A body json.loads parsed can still be too deep for json.dumps (on Python 3.14 the two have different limits), and
    # the RecursionError out of run() ended the bridge with no answer. No Python from 3.11 to 3.14 can encode this.
    import app.mcp_stdio as bridge

    nested: list = []
    for _ in range(1_000_000):
        nested = [nested]
    answers = [
        {"jsonrpc": "2.0", "id": 7, "result": nested},
        {"jsonrpc": "2.0", "id": 8, "result": {"ok": True}},
    ]
    client = bridge.HttpMcpClient(base_url="http://controller.invalid/mcp", bearer_token=MARKER)
    proxy = bridge.StdioMcpBridge(client=client, stderr=io.StringIO())
    monkeypatch.setattr(proxy, "_handle_line", lambda _line: answers.pop(0))
    stdout = io.StringIO()

    proxy.run(stdin=io.StringIO("first\nsecond\n"), stdout=stdout)

    first, second = (json.loads(line) for line in stdout.getvalue().splitlines())
    assert first == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {
            "code": -32000,
            "message": "The controller's answer is nested too deep for the stdio bridge to send.",
        },
    }
    assert second == {"jsonrpc": "2.0", "id": 8, "result": {"ok": True}}


def test_a_request_id_too_deep_to_encode_is_answered_with_a_null_id(monkeypatch) -> None:
    # The error for that request cannot carry the id either: JSON-RPC's null id says the request's could not be read.
    import app.mcp_stdio as bridge

    nested: list = []
    for _ in range(1_000_000):
        nested = [nested]
    client = bridge.HttpMcpClient(base_url="http://controller.invalid/mcp", bearer_token=MARKER)
    proxy = bridge.StdioMcpBridge(client=client, stderr=io.StringIO())
    monkeypatch.setattr(proxy, "_handle_line", lambda _line: {"jsonrpc": "2.0", "id": nested, "result": {}})
    stdout = io.StringIO()

    proxy.run(stdin=io.StringIO("line\n"), stdout=stdout)

    reply = json.loads(stdout.getvalue())
    assert reply["id"] is None
    assert reply["error"]["code"] == -32000


def test_a_stdin_line_too_deep_to_parse_is_answered_as_invalid_json() -> None:
    # json.loads raises RecursionError on this on every supported Python, which ended the bridge with no answer.
    import app.mcp_stdio as bridge

    depth = 1_000_000
    client = bridge.HttpMcpClient(base_url="http://controller.invalid/mcp", bearer_token=MARKER)
    proxy = bridge.StdioMcpBridge(client=client, stderr=io.StringIO())
    stdout = io.StringIO()

    proxy.run(stdin=io.StringIO("[" * depth + "]" * depth + "\n"), stdout=stdout)

    assert json.loads(stdout.getvalue()) == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Invalid JSON payload"},
    }


def test_a_number_too_long_to_parse_is_answered_as_invalid_json_and_the_bridge_goes_on() -> None:
    # CPython refuses an int of more than 4300 digits with a plain ValueError (not a JSONDecodeError), as a bare
    # number and as an id, and that ended the bridge with no answer.
    import app.mcp_stdio as bridge

    client = bridge.HttpMcpClient(base_url="http://controller.invalid/mcp", bearer_token=MARKER)
    proxy = bridge.StdioMcpBridge(client=client, stderr=io.StringIO())
    stdout = io.StringIO()
    digits = "9" * 5000
    lines = digits + "\n" + '{"jsonrpc": "2.0", "id": ' + digits + ', "method": "ping"}\n' + "[" * 9 + "\n"

    proxy.run(stdin=io.StringIO(lines), stdout=stdout)

    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert replies == [{"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON payload"}}] * 3


def test_r5c_20_the_unexpected_failure_on_stderr_never_shows_the_urls_secrets_or_the_token(monkeypatch) -> None:
    # The answer went through _shown; the line the bridge printed on stderr showed the error's text as it came.
    import app.mcp_stdio as bridge

    def fails(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"upstream http://controller.invalid/mcp?key=sentinel-0123 with {MARKER}")

    monkeypatch.setattr(bridge, "urlopen", fails)
    stderr = io.StringIO()
    client = bridge.HttpMcpClient(base_url="http://controller.invalid/mcp?key=sentinel-0123", bearer_token=MARKER)
    reply = bridge.StdioMcpBridge(client=client, stderr=stderr)._handle_line(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    )
    assert reply is not None

    shown = "upstream http://controller.invalid/mcp?[redacted] with [redacted]"
    assert stderr.getvalue() == f"stdio bridge unexpected error: {shown}\n"
    assert reply["error"]["message"] == f"Unexpected stdio bridge failure: {shown}"


def test_r5c_20_each_secret_is_replaced_once_and_a_paths_at_sign_is_kept() -> None:
    from app.mcp_stdio import _display_url, _redacted, _without_userinfo

    # A short secret was replaced again inside the [redacted] already written: "x [red[redacted]cted] ...".
    assert _redacted("x u:a y getaddrinfo failed", "http://u:a@h/mcp") == (
        "x [redacted] y get[redacted]ddrinfo f[redacted]iled"
    )
    # The display and the detail dropped any x@ run, a path's too: /a@b/mcp showed as /b/mcp.
    assert _display_url("http://controller.example/a@b/mcp?key=sentinel") == "http://controller.example/a@b/mcp"
    assert _without_userinfo("upstream http://controller.example/a@b/mcp failed") == (
        "upstream http://controller.example/a@b/mcp failed"
    )
    assert _without_userinfo("via http://alice:sentinel@proxy.invalid/x and 'bob@host'") == (
        "via http://proxy.invalid/x and 'host'"
    )


def test_r5c_20_a_query_value_of_8_characters_is_redacted_and_one_of_7_is_kept() -> None:
    # c3 left the boundary unpinned: every query value the other tests use is longer than 8 characters.
    from app.mcp_stdio import _redacted

    assert _redacted("short seven77, long eight888", "http://h/mcp?short=seven77&long=eight888") == (
        "short seven77, long [redacted]"
    )


def test_r5c_20_the_basic_credentials_httpx_sends_for_the_userinfo_are_redacted() -> None:
    # Review of c3's fixes (Codex 2): the client SDK shares this redaction, and httpx sends a base URL's userinfo as
    # "Authorization: Basic" and the base64 of user:password, each percent-decoded; a server echoing that header showed
    # the credentials. The bridges send no Basic header, so for them it only redacts more.
    from app.mcp_stdio import _redacted

    basic = base64.b64encode(b"alice:sentinel pass").decode()

    assert _redacted(f"illegal status line: Authorization: Basic {basic}", "http://alice:sentinel%20pass@h/mcp") == (
        "illegal status line: Authorization: Basic [redacted]"
    )


def test_r5c_20_each_form_of_the_url_given_counts_its_secrets() -> None:
    # Review of c3's fixes (Codex 3): the client SDK gives its base URL as configured and as httpx sends it, which
    # percent-encodes a space or a character outside ASCII in the query, so a server quoting the request line it got
    # quoted a form the configured text does not hold.
    from app.mcp_stdio import _redacted, _redacted_value

    forms = ("http://h/mcp?key=sentinel value", "http://h/mcp?key=sentinel%20value")

    assert _redacted("POST /mcp?key=sentinel%20value HTTP/1.1", forms) == "POST /mcp?[redacted] HTTP/1.1"
    assert _redacted_value({"input": "sentinel%20value"}, forms) == {"input": "[redacted]"}


def test_w15_1_the_unexpected_failure_answer_never_shows_the_urls_query(
    recording_proxy, request_line_echo: int
) -> None:
    # http.client's BadStatusLine quotes the request line, query and all, and is not a URLError: the generic answer
    # shows its text, which goes through _shown.
    message = _bridge_error_through_the_bridge(
        f"http://127.0.0.1:{request_line_echo}/mcp?key=sentinel", recording_proxy
    )

    assert "Unexpected stdio bridge failure: POST /mcp?[redacted] HTTP/1.1" in message
    assert "sentinel" not in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]


def test_w15_1_the_unreachable_answer_never_shows_the_urls_query(monkeypatch) -> None:
    # A URLError's reason can quote the URL (a proxy's tunnel failure, say): the answer that no controller is
    # reachable shows it through _shown.
    import app.mcp_stdio as bridge

    def unreachable(*_args: object, **_kwargs: object) -> None:
        raise URLError("tunnel to http://controller.invalid/mcp?key=sentinel failed")

    monkeypatch.setattr(bridge, "urlopen", unreachable)
    client = bridge.HttpMcpClient(base_url="http://controller.invalid/mcp?key=sentinel", bearer_token=MARKER)
    reply = bridge.StdioMcpBridge(client=client, stderr=io.StringIO())._handle_line(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    )
    assert reply is not None
    message = reply["error"]["message"]

    assert UNREACHABLE in message
    assert "(underlying: tunnel to http://controller.invalid/mcp?[redacted] failed)" in message
    assert "sentinel" not in message


def test_w14_1_the_error_text_shown_drops_the_urls_known_secrets() -> None:
    from app.mcp_stdio import _redacted

    shown = _redacted(
        "nonnumeric port: 'sentinel tail@controller.invalid' for key=sentinel",
        "http://alice:sentinel%20tail@controller.invalid/mcp?key=sentinel",
    )

    assert shown == "nonnumeric port: 'controller.invalid' for [redacted]"


def test_w14_1_dropping_userinfo_runs_is_linear() -> None:
    from app.mcp_stdio import _without_userinfo

    # CPU seconds, not wall seconds (R5C-29): a suspended or overloaded runner can spend 2 wall seconds on anything. The
    # linear scan takes milliseconds of CPU here; the quadratic one took about 7 CPU seconds for half this text.
    started = time.process_time()
    _without_userinfo("a" * 65536)

    assert time.process_time() - started < 2


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


@pytest.mark.parametrize("base_url", ["http://:8000/mcp", "http://user@:8000/mcp"])
def test_w15_8_an_empty_host_is_refused(recording_proxy, base_url: str) -> None:
    # Codex R01: urllib reads the host as ":8000" (not empty), so W15-3 let these through, and with no reading local
    # the request went to the environment proxy with the token (an empty HOST in http://${HOST}:${PORT}/mcp).
    message = _bridge_error_through_the_bridge(base_url, recording_proxy)

    assert INVALID in message
    assert "its host is empty" in message
    assert recording_proxy.seen == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "base_url",
    [
        "http://alice:93847561029384756102@controller.invalid:8000/mcp",
        "http://controller.invalid/mcp#93847561029384756102",
    ],
)
def test_w15_8_a_retry_after_equal_to_a_secret_of_the_url_is_not_shown(recording_proxy, base_url: str) -> None:
    # Codex R03: a numeric Retry-After passed the seconds check whole, even as the URL's password or fragment.
    recording_proxy.answer = (  # type: ignore[attr-defined]
        429,
        json.dumps({"detail": "Too many requests"}).encode(),
        {"Retry-After": "93847561029384756102"},
    )

    message = _bridge_error_through_the_bridge(base_url, recording_proxy)

    assert message == "Auto Browser controller returned HTTP 429: Too many requests (rate limited)"
