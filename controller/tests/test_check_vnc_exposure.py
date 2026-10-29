"""scripts/check_vnc_exposure.py must not pass a node it did not check.

Its takeover step reported success as soon as x11vnc offered no security type None, before any
authentication or ServerInit: a server advertising only VNC authentication passed, and so did a
node meant to run without a password that unexpectedly asked for one. The expectation is now
explicit (--expect-password), and each mode fails on the other's handshake.

A stand-in for websockify plus x11vnc serves the WebSocket and RFB 3.8 exchange on 127.0.0.1.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import socket
import struct
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_vnc_exposure.py"
_WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_NONE, _VNC_AUTH, _TIGHT = 1, 2, 16

pytestmark = pytest.mark.skipif(not _SCRIPT.is_file(), reason="scripts/ is not shipped in the controller image")


def _load_check():
    spec = importlib.util.spec_from_file_location("check_vnc_exposure_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeNoVnc:
    """noVNC's websockify (Origin must equal http://Host) in front of an x11vnc offering `security_types`."""

    def __init__(self, security_types: list[int]) -> None:
        self.security_types = bytes(security_types)
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        self.server.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:  # closed by the test
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn, contextlib.suppress(ConnectionError, OSError):  # the client may hang up early
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            head, _, pending = request.partition(b"\r\n\r\n")
            headers = dict(line.split(": ", 1) for line in head.decode("ascii").split("\r\n")[1:])
            if headers.get("Origin") != f"http://{headers.get('Host')}":
                conn.sendall(b"HTTP/1.1 403 Invalid Origin Header\r\nContent-Length: 0\r\n\r\n")
                return
            key = headers["Sec-WebSocket-Key"].encode() + _WEBSOCKET_GUID
            accept = base64.b64encode(hashlib.sha1(key, usedforsecurity=False).digest())  # RFC 6455 accept value
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept + b"\r\nSec-WebSocket-Protocol: binary\r\n\r\n"
            )
            buffer = bytearray(pending)
            self._send(conn, b"RFB 003.008\n")
            assert self._receive(conn, buffer) == b"RFB 003.008\n"
            self._send(conn, bytes([len(self.security_types)]) + self.security_types)
            if self._receive(conn, buffer) != bytes([_NONE]):
                return  # VNC authentication would send a challenge; the check never goes that far
            self._send(conn, struct.pack(">I", 0))
            assert self._receive(conn, buffer) == b"\x01"  # ClientInit, shared
            name = b"browser-node:99"
            self._send(conn, struct.pack(">HH", 1280, 800) + bytes(16) + struct.pack(">I", len(name)) + name)

    @staticmethod
    def _send(conn: socket.socket, data: bytes) -> None:
        conn.sendall(bytes([0x82, len(data)]) + data)

    @staticmethod
    def _receive(conn: socket.socket, buffer: bytearray) -> bytes:
        def take(size: int) -> bytes:
            while len(buffer) < size:
                chunk = conn.recv(4096)
                if not chunk:
                    raise ConnectionError("client closed")
                buffer.extend(chunk)
            out = bytes(buffer[:size])
            del buffer[:size]
            return out

        _, second = take(2)
        mask = take(4)
        return bytes(byte ^ mask[i % 4] for i, byte in enumerate(take(second & 0x7F)))


class TakeoverHandshakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.check = _load_check()

    def serve(self, security_types: list[int]) -> int:
        server = FakeNoVnc(security_types)
        self.addCleanup(server.close)
        return server.port

    def test_a_node_without_a_password_reaches_server_init(self) -> None:
        said = self.check.takeover("127.0.0.1", self.serve([_NONE]))
        self.assertTrue(said.endswith("display 1280x800 'browser-node:99'"), said)

    def test_a_node_without_a_password_fails_when_it_asks_for_one(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "does not offer security type None"):
            self.check.takeover("127.0.0.1", self.serve([_VNC_AUTH]))

    def test_expect_password_requires_vnc_authentication_and_no_none(self) -> None:
        said = self.check.takeover("127.0.0.1", self.serve([_VNC_AUTH]), expect_password=True)
        self.assertIn("security type None not offered, VNC authentication (type 2) offered", said)

    def test_expect_password_fails_when_none_is_still_offered(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "offers security type None"):
            self.check.takeover("127.0.0.1", self.serve([_NONE, _VNC_AUTH]), expect_password=True)

    def test_expect_password_fails_without_vnc_authentication(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "does not offer VNC authentication"):
            self.check.takeover("127.0.0.1", self.serve([_TIGHT]), expect_password=True)

    def run_main(self, port: int, *flags: str) -> int:
        """main() with the in-container half stubbed out as passing."""
        passed = type("Completed", (), {"returncode": 0})()
        with patch.object(self.check.subprocess, "run", return_value=passed), contextlib.redirect_stdout(io.StringIO()):
            return self.check.main(["browser-node", str(port), *flags])

    def test_the_command_line_flag_selects_the_expectation(self) -> None:
        password_node = self.serve([_VNC_AUTH])
        self.assertEqual(self.run_main(password_node, "--expect-password"), 0)
        self.assertEqual(self.run_main(password_node), 1)
        open_node = self.serve([_NONE])
        self.assertEqual(self.run_main(open_node), 0)
        self.assertEqual(self.run_main(open_node, "--expect-password"), 1)
