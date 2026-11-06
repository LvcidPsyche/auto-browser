"""The host bridge: a total deadline on the body, a cap on concurrent codex runs, group kill, and quiet errors.

HARN-45: the per-read socket timeout restarts on every byte, there was no limit on concurrent codex runs, and a
timeout killed only the direct codex child. HARN-46: a non-UTF-8 body answered 500 with the exception text, and codex's
stderr went back to the caller on success.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import select
import signal
import socket
import socketserver
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "codex_host_bridge.py"


def _load_bridge():
    """Load the script under a private name; UnixStreamServer is missing on Windows (import only)."""
    shimmed = not hasattr(socketserver, "UnixStreamServer")
    if shimmed:
        socketserver.UnixStreamServer = socketserver.TCPServer
    try:
        spec = importlib.util.spec_from_file_location("codex_host_bridge_hardening_under_test", _SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if shimmed:
            del socketserver.UnixStreamServer


bridge = _load_bridge() if _SCRIPT.is_file() else None
PAYLOAD = {"prompt": "decide", "schema": {"type": "object"}}


class _Service:
    def __init__(self, error: Exception | None = None) -> None:
        self.payloads: list[dict] = []
        self.error = error

    def handle_openai_decide(self, payload):
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return {"ok": True}


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, service) -> None:
        self.service = service
        super().__init__(("127.0.0.1", 0), bridge.BridgeRequestHandler)


class _FakeCodex:
    """Stands in for subprocess.Popen: writes the decision file the command names, and can block until released."""

    stderr_text = b""
    gate: threading.Event | None = None
    entered = threading.Semaphore(0)

    def __init__(self, command, **kwargs) -> None:
        self.command = self.args = command
        self.returncode = 0
        self.pid = 0
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text('{"action": "done"}', encoding="utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def communicate(self, input=None, timeout=None):
        type(self).entered.release()
        if type(self).gate is not None:
            type(self).gate.wait(10)
        return b"", type(self).stderr_text

    def kill(self) -> None:
        return None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def _raw_post(port: int, headers: str, body: bytes = b"", *, read_timeout: float = 5.0, then=None) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=read_timeout) as client:
        client.sendall(f"POST /openai/decide HTTP/1.1\r\nHost: x\r\n{headers}\r\n".encode() + body)
        if then is not None:
            then(client)
        chunks: list[bytes] = []
        while True:
            data = client.recv(4096)
            if not data:
                break
            chunks.append(data)
            if b"\r\n\r\n" in b"".join(chunks) and b"}" in chunks[-1]:
                break
        return b"".join(chunks)


class _ThreadingWithTimer:
    """Stands in for the bridge module's `threading` name only, so no other module picks up the fake: a Timer whose
    callback belongs to `owner` (the test's own handler) is the given fake, and everything else is the real module.

    A keep-alive BridgeRequestHandler thread another test in this module left running reads this same patched name;
    the Timer it asks for has its own handler's callback, so it gets a real one and never lands in the capture.
    """

    def __init__(self, timer, owner) -> None:
        self._timer = timer
        self._owner = owner

    def Timer(self, interval, function, args=None, kwargs=None):  # noqa: N802 - threading.Timer's own name
        if getattr(function, "__self__", None) is self._owner:
            return self._timer(interval, function, args, kwargs)
        return threading.Timer(interval, function, args, kwargs)

    def __getattr__(self, name: str):
        return getattr(threading, name)


class _ShutDownConnection:
    """A handler's socket as the header deadline sees it: shutdown() is recorded."""

    def __init__(self) -> None:
        self.shut_down = False

    def shutdown(self, how: int) -> None:
        self.shut_down = True


class _BrokenWriter:
    """wfile on a socket that was shut down: a write is a broken pipe (or a reset), as on POSIX."""

    def __init__(self, connection: _ShutDownConnection, error: type[OSError]) -> None:
        self.connection = connection
        self.error = error

    def write(self, data: bytes) -> int:
        if self.connection.shut_down:
            raise self.error(32, "Broken pipe")
        return len(data)

    def flush(self) -> None:
        return None


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
class BridgeErrorReplyTests(unittest.TestCase):
    def _serve(self, service) -> int:
        server = _Server(service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_a_body_that_is_not_utf8_is_a_400_with_a_fixed_message(self) -> None:
        service = _Service()
        port = self._serve(service)
        body = b'{"prompt": "\xff\xfe"}'

        response = _raw_post(port, f"Content-Length: {len(body)}\r\n", body)

        self.assertIn(b"400", response.split(b"\r\n", 1)[0])
        self.assertIn(b"UTF-8", response)
        self.assertEqual(service.payloads, [])

    def test_an_unexpected_error_is_logged_and_answered_with_a_fixed_body(self) -> None:
        port = self._serve(_Service(error=RuntimeError("secret detail at /home/someone/.codex")))
        body = json.dumps(PAYLOAD).encode()
        captured = io.StringIO()

        with redirect_stderr(captured):
            response = _raw_post(port, f"Content-Length: {len(body)}\r\n", body)

        self.assertIn(b"500", response.split(b"\r\n", 1)[0])
        self.assertEqual(json.loads(response.split(b"\r\n\r\n", 1)[1]), {"error": "internal error"})
        self.assertNotIn(b"secret detail", response)
        self.assertIn("secret detail", captured.getvalue())

    def test_codex_stderr_is_logged_and_not_returned(self) -> None:
        _FakeCodex.stderr_text = b"warning: token sk-secret in config"
        _FakeCodex.gate = None
        service = bridge.CodexBridgeService(codex_path="codex")
        captured = io.StringIO()

        with patch.object(bridge.subprocess, "Popen", _FakeCodex), redirect_stderr(captured):
            reply = service.handle_openai_decide(dict(PAYLOAD))

        self.assertEqual(reply["raw_text"], '{"action": "done"}')
        self.assertNotIn("stderr", reply)
        self.assertIn("warning: token", captured.getvalue())

    def test_a_failed_codex_run_answers_a_fixed_502_and_logs_its_output_on_the_bridge(self) -> None:
        # R-19: a nonzero exit answered 502 with up to 4,000 characters of Codex's stderr or stdout.
        for stream in ("stderr", "stdout"):
            with self.subTest(stream=stream):

                class _FailingCodex(_FakeCodex):
                    def __init__(self, command, **kwargs) -> None:
                        super().__init__(command, **kwargs)
                        Path(command[command.index("--output-last-message") + 1]).unlink()
                        self.returncode = 1

                    def communicate(self, input=None, timeout=None):
                        secret = b"sentinel-secret-6f1d at /home/someone/.codex/auth.json"
                        return (b"", secret) if stream == "stderr" else (secret, b"")

                service = bridge.CodexBridgeService(codex_path="codex")
                captured = io.StringIO()

                with (
                    patch.object(bridge.subprocess, "Popen", _FailingCodex),
                    redirect_stderr(captured),
                    self.assertRaises(bridge.BridgeError) as raised,
                ):
                    service.handle_openai_decide(dict(PAYLOAD))

                self.assertEqual(raised.exception.status_code, 502)
                self.assertNotIn("sentinel-secret", str(raised.exception))
                self.assertEqual(str(raised.exception), "codex exited with 1; see the bridge log")
                self.assertIn("sentinel-secret-6f1d", captured.getvalue())

    def test_a_failed_codex_run_with_no_output_logs_its_exit_code(self) -> None:
        # CB2-5: the reply says "see the bridge log", which then held nothing for a silent failure.
        class _SilentCodex(_FakeCodex):
            def __init__(self, command, **kwargs) -> None:
                super().__init__(command, **kwargs)
                Path(command[command.index("--output-last-message") + 1]).unlink()
                self.returncode = 3

            def communicate(self, input=None, timeout=None):
                return b"", b"  \n"

        service = bridge.CodexBridgeService(codex_path="codex")
        captured = io.StringIO()
        with (
            patch.object(bridge.subprocess, "Popen", _SilentCodex),
            redirect_stderr(captured),
            self.assertRaises(bridge.BridgeError) as raised,
        ):
            service.handle_openai_decide(dict(PAYLOAD))

        self.assertEqual(str(raised.exception), "codex exited with 3; see the bridge log")
        self.assertIn("[codex-bridge] codex exited with 3 and printed nothing", captured.getvalue())


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
class BridgeLimitsTests(unittest.TestCase):
    def test_a_client_that_drips_bytes_is_cut_off_by_the_total_deadline(self) -> None:
        server = _Server(_Service())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # Each byte arrives inside the 0.3 s per-read timeout; only the total deadline can end this.
        self.addCleanup(setattr, bridge.BridgeRequestHandler, "timeout", bridge.BridgeRequestHandler.timeout)
        bridge.BridgeRequestHandler.timeout = 0.3

        def drip(client: socket.socket) -> None:
            for _ in range(30):
                # Wait 0.15 s for the server's answer; once it speaks, stop sending (more bytes would reset the
                # connection and could discard the answer before it is read).
                if select.select([client], [], [], 0.15)[0]:
                    return
                try:
                    client.sendall(b"x")
                except OSError:
                    return

        with patch.object(bridge, "REQUEST_BODY_DEADLINE_SECONDS", 0.6, create=True):
            started = time.monotonic()
            response = _raw_post(server.server_address[1], "Content-Length: 100\r\n", b"{", read_timeout=8.0, then=drip)

        self.assertIn(b"408", response.split(b"\r\n", 1)[0])
        self.assertLess(time.monotonic() - started, 4.0)

    def test_a_client_that_drips_header_bytes_is_cut_off_by_the_header_deadline(self) -> None:
        server = _Server(_Service())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # The headers never end and each byte arrives inside the 0.3 s per-read timeout: only a total deadline on
        # the request line and headers frees the handler thread.
        self.addCleanup(setattr, bridge.BridgeRequestHandler, "timeout", bridge.BridgeRequestHandler.timeout)
        bridge.BridgeRequestHandler.timeout = 0.3

        with (
            patch.object(bridge, "REQUEST_HEADER_DEADLINE_SECONDS", 0.6, create=True),
            socket.create_connection(server.server_address, timeout=8.0) as client,
        ):
            started = time.monotonic()
            client.sendall(b"POST /openai/decide HTTP/1.1\r\nHost: x\r\nX-Drip: ")
            for _ in range(30):
                if select.select([client], [], [], 0.15)[0]:
                    break  # the server closed the connection (or spoke)
                try:
                    client.sendall(b"a")
                except OSError:
                    break
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0)

    def test_the_per_read_timeout_is_restored_after_a_slow_body(self) -> None:
        # The body's deadline shrinks the socket timeout as it nears. The reply and the next keep-alive request must
        # get the full per-read timeout back, not the few milliseconds the body left.
        handler = bridge.BridgeRequestHandler.__new__(bridge.BridgeRequestHandler)
        timeouts: list[float] = []
        handler.connection = SimpleNamespace(settimeout=timeouts.append)
        handler.rfile = io.BytesIO(b"{}")
        clock = iter([0.0, 59.99, 59.99, 59.99])

        with patch.object(bridge, "time", SimpleNamespace(monotonic=lambda: next(clock))):
            body = handler._read_exactly(2)

        self.assertEqual(body, b"{}")
        self.assertLess(timeouts[0], 1.0)
        self.assertEqual(timeouts[-1], handler.timeout)

    def test_decisions_past_the_concurrency_limit_are_answered_503(self) -> None:
        _FakeCodex.stderr_text = b""
        _FakeCodex.gate = threading.Event()
        _FakeCodex.entered = threading.Semaphore(0)
        self.addCleanup(setattr, _FakeCodex, "gate", None)
        service = bridge.CodexBridgeService(codex_path="codex", max_concurrent_decisions=2)
        outcomes: list[object] = []

        def call() -> None:
            try:
                outcomes.append(service.handle_openai_decide(dict(PAYLOAD)))
            except bridge.BridgeError as exc:
                outcomes.append(exc)

        with (
            patch.object(bridge.subprocess, "Popen", _FakeCodex),
            patch.object(bridge, "DECISION_SLOT_WAIT_SECONDS", 0.2, create=True),
        ):
            first = [threading.Thread(target=call) for _ in range(2)]
            for thread in first:
                thread.start()
            for _ in range(2):
                self.assertTrue(_FakeCodex.entered.acquire(timeout=5))
            call()  # the third, while two are held open
            _FakeCodex.gate.set()
            for thread in first:
                thread.join(5)

        busy = [item for item in outcomes if isinstance(item, bridge.BridgeError)]
        self.assertEqual([item.status_code for item in busy], [503])
        self.assertEqual(len([item for item in outcomes if isinstance(item, dict)]), 2)


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
class BridgeConnectionCapTests(unittest.TestCase):
    """Each connection holds a handler thread, and a timer thread while its headers come in. Past the cap a new
    connection is answered 503 and closed at once instead of starting more."""

    def _server(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        if bridge.UnixHTTPServer.address_family == socket.AF_INET:  # loaded with TCPServer in place: no AF_UNIX here
            address = ("127.0.0.1", 0)
        else:
            address = str(Path(tmp.name) / "bridge.sock")
        server = bridge.UnixHTTPServer(address, _Service())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def _connect(self, server, *, timeout: float = 5.0) -> socket.socket:
        client = socket.socket(server.address_family, socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(server.server_address)
        # Before the server's cleanups run, so each handler sees its client gone and ends. server_close joins none of
        # them: UnixHTTPServer sets daemon_threads, and the stdlib does not track daemon handler threads.
        self.addCleanup(client.close)
        return client

    @staticmethod
    def _read_to_end(client: socket.socket) -> bytes:
        chunks: list[bytes] = []
        try:
            while data := client.recv(4096):
                chunks.append(data)
        except ConnectionResetError:
            pass  # a refused client that had already sent its request can see a reset after the reply
        return b"".join(chunks)

    def test_a_connection_past_the_cap_is_answered_503_and_a_freed_slot_is_used_again(self) -> None:
        with patch.object(bridge, "MAX_CONNECTIONS", 2, create=True):
            server = self._server()
        held = [self._connect(server) for _ in range(2)]  # two handlers wait on request lines that never come

        reply = self._read_to_end(self._connect(server, timeout=2.0))

        self.assertTrue(reply.startswith(b"HTTP/1.1 503 "), reply[:80])
        self.assertIn(b"Connection: close", reply)

        held[0].close()  # its handler finishes and gives its slot back
        deadline = time.monotonic() + 5.0
        while True:
            client = self._connect(server)
            try:
                client.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                # The slot was not free yet: the 503 and its close came before the request went out. Try again.
                reply = b""
            else:
                reply = self._read_to_end(client)
            if reply.startswith(b"HTTP/1.1 200 ") or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        self.assertTrue(reply.startswith(b"HTTP/1.1 200 "), reply[:80])

    def test_the_cap_is_a_named_constant(self) -> None:
        self.assertEqual(bridge.MAX_CONNECTIONS, 32)


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
class BridgeHeaderDeadlineRaceTests(unittest.TestCase):
    """Timer.cancel() cannot stop a callback that is already running: the deadline firing just as the headers come
    in must not shut down a request that parse_request has accepted."""

    def test_a_deadline_that_fires_as_the_headers_complete_leaves_the_request_alone(self) -> None:
        captured: list[tuple] = []

        class _CapturedTimer:
            def __init__(self, interval, function, args=None, kwargs=None) -> None:
                captured.append((function, tuple(args or ()), dict(kwargs or {})))
                self.daemon = False

            def start(self) -> None:
                return None

            def cancel(self) -> None:
                return None  # too late: the timer thread is past its check and runs the callback below

        class _StdlibStandIn(bridge.BaseHTTPRequestHandler):
            """Sits between BridgeRequestHandler and the stdlib in _Handler's MRO, so the bridge's super() calls
            reach these for that class alone: no other handler in the process sees them."""

            def handle_one_request(self) -> None:
                self.parse_request()

            def parse_request(self) -> bool:
                return True

        class _Handler(bridge.BridgeRequestHandler, _StdlibStandIn):
            pass

        handler = object.__new__(_Handler)
        handler.connection = Mock()
        handler.close_connection = False
        real_timer, real_parse = threading.Timer, bridge.BaseHTTPRequestHandler.parse_request

        with patch.object(bridge, "threading", _ThreadingWithTimer(_CapturedTimer, handler)):
            # A handler thread another test left running looks these up: it must see the real ones.
            self.assertIs(threading.Timer, real_timer)
            self.assertIs(bridge.BaseHTTPRequestHandler.parse_request, real_parse)
            handler.handle_one_request()
            self.assertEqual(len(captured), 1)  # this handler's own deadline, and nobody else's
            function, args, kwargs = captured[0]
            function(*args, **kwargs)

        handler.connection.shutdown.assert_not_called()
        self.assertFalse(handler.close_connection)


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
class BridgeHeaderDeadlineFragmentTests(unittest.TestCase):
    """The deadline shuts the connection down under a request line still coming in. The stdlib then answers what had
    arrived with a 400, and that write fails: the deadline's doing, not an error to escape the handler."""

    def _handle(self, line: bytes, *, deadline_passes: bool, client_gone: bool = False, error=BrokenPipeError):
        captured: list[tuple] = []

        class _CapturedTimer:
            def __init__(self, interval, function, args=None, kwargs=None) -> None:
                captured.append((function, tuple(args or ())))
                self.daemon = False

            def start(self) -> None:
                return None

            def cancel(self) -> None:
                return None

        class _StalledLine:
            def readline(self, limit: int = -1) -> bytes:
                if deadline_passes:
                    function, args = captured[0]
                    function(*args)  # the deadline passes while the line is still coming in
                return line  # what had arrived when the shutdown ended the read

        handler = object.__new__(bridge.BridgeRequestHandler)
        connection = _ShutDownConnection()
        connection.shut_down = client_gone
        handler.connection = connection
        handler.rfile = _StalledLine()
        handler.wfile = _BrokenWriter(connection, error)
        handler.close_connection = False
        with (
            patch.object(bridge, "threading", _ThreadingWithTimer(_CapturedTimer, handler)),
            redirect_stderr(io.StringIO()),
        ):
            try:
                handler.handle_one_request()
            finally:
                self.assertEqual(len(captured), 1)  # this handler's own deadline, and nobody else's
        return handler, connection

    def test_a_request_line_cut_off_by_the_deadline_ends_without_an_error(self) -> None:
        for line in (b"POST /openai/dec", b"POST", b"POST /openai/decide HTTP/1."):
            for error in (BrokenPipeError, ConnectionResetError):
                with self.subTest(line=line, error=error.__name__):
                    handler, connection = self._handle(line, deadline_passes=True, error=error)
                    self.assertTrue(connection.shut_down)
                    self.assertTrue(handler.close_connection)

    def test_a_broken_pipe_the_deadline_did_not_cause_still_raises(self) -> None:
        # The client went away while the bridge answered a complete, bad request line: not the deadline's doing.
        for error in (BrokenPipeError, ConnectionResetError):
            with self.subTest(error=error.__name__), self.assertRaises(error):
                self._handle(b"POST /openai/decide\r\n", deadline_passes=False, client_gone=True, error=error)


class _RunningCodex(_FakeCodex):
    """A fake codex with Popen's returncode: None until communicate() has returned."""

    def __init__(self, command, **kwargs) -> None:
        super().__init__(command, **kwargs)
        self.returncode = None

    def communicate(self, input=None, timeout=None):
        output = super().communicate(input, timeout)
        self.returncode = 0
        return output


class _ReapableRun:
    """A codex run the sweep finds in flight. reap() is the handler thread's communicate() collecting its exit."""

    def __init__(self) -> None:
        self.returncode = None
        self.pid = 0

    def reap(self) -> None:
        self.returncode = 0

    def poll(self):
        return self.returncode


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
class BridgeShutdownTests(unittest.TestCase):
    """HARN-45: codex runs in its own session, so stopping a hand-run bridge (Ctrl+C) left it running, orphaned."""

    def test_a_run_reaped_after_the_sweep_took_its_list_is_not_killed(self) -> None:
        # The sweep lists the runs under the lock and kills them after releasing it, so communicate() can reap one in
        # between, and its pid is then free for the system to reuse.
        service = bridge.CodexBridgeService(codex_path="codex")
        first, second = _ReapableRun(), _ReapableRun()
        service._in_flight.update({first: True, second: True})
        killed: list[object] = []

        def kill(process, grouped) -> None:
            killed.append(process)
            second.reap()  # its communicate() returns while the first run is being killed

        with patch.object(bridge, "_kill_process_tree", side_effect=kill):
            stopped = service.kill_in_flight()

        self.assertEqual(killed, [first])
        self.assertEqual(stopped, 1)

    def test_the_codex_runs_in_flight_can_be_killed(self) -> None:
        _FakeCodex.stderr_text = b""
        _FakeCodex.gate = threading.Event()
        _FakeCodex.entered = threading.Semaphore(0)
        self.addCleanup(setattr, _FakeCodex, "gate", None)
        service = bridge.CodexBridgeService(codex_path="codex")
        killed: list[tuple[object, bool]] = []

        with (
            patch.object(bridge.subprocess, "Popen", _RunningCodex),
            patch.object(
                bridge, "_kill_process_tree", side_effect=lambda process, grouped: killed.append((process, grouped))
            ),
        ):
            worker = threading.Thread(target=service.handle_openai_decide, args=(dict(PAYLOAD),))
            worker.start()
            self.assertTrue(_FakeCodex.entered.acquire(timeout=5))
            stopped = service.kill_in_flight()
            _FakeCodex.gate.set()
            worker.join(5)

        self.assertEqual(stopped, 1)
        self.assertEqual([grouped for _process, grouped in killed], [hasattr(os, "killpg")])
        self.assertEqual(service.kill_in_flight(), 0)  # a finished run is no longer tracked

    def test_a_decision_that_reaches_codex_after_the_sweep_does_not_start_it(self) -> None:
        # Handler threads are daemon threads: one between its body read and Popen when the sweep ran would start a
        # codex nothing kills once main() returns.
        _FakeCodex.stderr_text = b""
        started: list[list[str]] = []
        service = bridge.CodexBridgeService(codex_path="codex")

        def popen(command, **kwargs):
            started.append(command)
            return _RunningCodex(command, **kwargs)

        with patch.object(bridge.subprocess, "Popen", side_effect=popen):
            self.assertEqual(service.kill_in_flight(), 0)
            with self.assertRaises(bridge.BridgeError) as raised:
                service.handle_openai_decide(dict(PAYLOAD))

        self.assertEqual(started, [])
        self.assertEqual(raised.exception.status_code, 503)

    def test_killing_the_runs_in_flight_ends_a_real_child(self) -> None:
        # A real process this time: its own process group on POSIX (killpg), the process itself on Windows.
        service = bridge.CodexBridgeService(codex_path="codex")
        results: list[tuple[int, bytes, bytes]] = []
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        worker = threading.Thread(target=lambda: results.append(service._run_codex(sleeper, b"")))
        worker.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not service._in_flight:  # the sweep would stop the service: wait first
            time.sleep(0.05)
        self.assertEqual(service.kill_in_flight(), 1)
        worker.join(10)

        self.assertFalse(worker.is_alive())
        self.assertNotEqual(results[0][0], 0)

    def test_main_kills_the_runs_in_flight_when_the_server_stops(self) -> None:
        calls: list[str] = []

        class _StoppedServer:
            def __init__(self, socket_path, service) -> None:
                Path(socket_path).touch()

            def serve_forever(self) -> None:
                calls.append("served")  # returns at once, as it does after SIGINT or SIGTERM

            def server_close(self) -> None:
                calls.append("closed")

        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "codex.sock"
            args = SimpleNamespace(
                socket_path=str(socket_path), codex_path="codex", model=None, request_timeout_seconds=55.0, pass_env=[]
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(bridge, "parse_args", return_value=args),
                patch.object(bridge, "prepare_socket_path", return_value=socket_path),
                patch.object(bridge, "UnixHTTPServer", _StoppedServer),
                patch.object(bridge.signal, "signal"),
                patch.object(bridge.os, "umask"),
                patch.object(
                    bridge.CodexBridgeService,
                    "kill_in_flight",
                    create=True,
                    side_effect=lambda: calls.append("killed") or 0,
                ),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(bridge.main(), 0)

        self.assertEqual(calls, ["served", "killed", "closed"])


@unittest.skipUnless(
    bridge is not None and hasattr(os, "killpg") and sys.platform != "darwin", "needs POSIX process groups"
)
class BridgeProcessGroupTests(unittest.TestCase):
    def test_a_timeout_kills_the_children_codex_started_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            fake = Path(tmp) / "fake-codex"
            fake.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {pid_file}\nwait\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            service = bridge.CodexBridgeService(codex_path=str(fake), request_timeout_seconds=1.0)

            with self.assertRaises(bridge.BridgeError) as caught:
                service.handle_openai_decide(dict(PAYLOAD))

            self.assertEqual(caught.exception.status_code, 504)
            child = int(pid_file.read_text().strip())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _is_running(child):
                time.sleep(0.05)
            self.assertFalse(_is_running(child), "the grandchild outlived the timeout")

    def test_the_shutdown_sweep_reaches_the_group_of_a_leader_that_already_exited(self) -> None:
        # R-10: poll() reaped the exited leader, so the sweep skipped the group its children still held the pipes of.
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            fake = Path(tmp) / "fake-codex"
            fake.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {pid_file}\nexit 0\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            service = bridge.CodexBridgeService(codex_path="codex", request_timeout_seconds=60.0)
            worker = threading.Thread(target=service._run_codex, args=([str(fake)], b""), daemon=True)
            worker.start()
            deadline = time.monotonic() + 10
            leader = None
            while time.monotonic() < deadline:
                with service._in_flight_lock:
                    leader = next(iter(service._in_flight), None)
                if leader is not None and pid_file.exists() and pid_file.read_text().strip():
                    if not _is_running(leader.pid):  # exited, not reaped: a zombie
                        break
                time.sleep(0.05)
            if pid_file.exists() and pid_file.read_text().strip():
                # Registered before any assert: a failing one must not leave `sleep 60` running.
                self.addCleanup(_kill_quietly, int(pid_file.read_text().strip()))
            self.assertIsNotNone(leader)
            self.assertFalse(_is_running(leader.pid), "the fake codex leader never exited")
            child = int(pid_file.read_text().strip())

            self.assertEqual(service.kill_in_flight(), 1)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _is_running(child):
                time.sleep(0.05)
            self.assertFalse(_is_running(child), "the child of the exited leader outlived the sweep")
            worker.join(5)
            self.assertFalse(worker.is_alive())


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie nobody has reaped yet is not running
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
