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
from unittest.mock import patch

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
