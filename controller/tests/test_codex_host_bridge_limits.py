"""The host bridge reads a request body into memory: it needs a size cap and a read timeout.

M-15: a 64 MB Content-Length was read and parsed, a negative one made rfile.read(-1)
wait for the client to close, and a client that stalled mid-request held a handler
thread forever. The socket is mode 0600, so this is hardening against a confused
local process rather than a remote hole.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import socketserver
import sys
import threading
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "codex_host_bridge.py"


def _load_bridge():
    """Load the script under a private name; UnixStreamServer is missing on Windows (import only)."""
    shimmed = not hasattr(socketserver, "UnixStreamServer")
    if shimmed:
        socketserver.UnixStreamServer = socketserver.TCPServer
    try:
        spec = importlib.util.spec_from_file_location("codex_host_bridge_limits_under_test", _SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if shimmed:
            del socketserver.UnixStreamServer


bridge = _load_bridge() if _SCRIPT.is_file() else None


class _Service:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def handle_openai_decide(self, payload):
        self.payloads.append(payload)
        return {"ok": True}


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, service) -> None:
        self.service = service
        super().__init__(("127.0.0.1", 0), bridge.BridgeRequestHandler)


@unittest.skipIf(bridge is None, "scripts/ is not shipped in the controller image")
class BridgeBodyLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _Service()
        self.server = _Server(self.service)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _exchange(self, raw: bytes, *, read_timeout: float = 5.0) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=read_timeout) as client:
            client.sendall(raw)
            chunks = []
            while True:
                data = client.recv(4096)
                if not data:
                    break
                chunks.append(data)
                if b"\r\n\r\n" in b"".join(chunks) and b"}" in chunks[-1]:
                    break
            return b"".join(chunks)

    @staticmethod
    def _post(headers: str, body: bytes = b"") -> bytes:
        return f"POST /openai/decide HTTP/1.1\r\nHost: x\r\n{headers}\r\n".encode() + body

    def test_the_limits_are_named_constants_of_sensible_size(self) -> None:
        self.assertGreaterEqual(bridge.MAX_REQUEST_BODY_BYTES, 16 * 1024 * 1024)
        self.assertLessEqual(bridge.MAX_REQUEST_BODY_BYTES, 32 * 1024 * 1024)
        self.assertEqual(bridge.BridgeRequestHandler.timeout, bridge.REQUEST_READ_TIMEOUT_SECONDS)

    def test_an_oversize_content_length_is_refused_without_reading_the_body(self) -> None:
        declared = bridge.MAX_REQUEST_BODY_BYTES + 1
        response = self._exchange(self._post(f"Content-Length: {declared}\r\n"))

        self.assertIn(b"413", response.split(b"\r\n", 1)[0])
        self.assertIn(b"exceeds the", response)
        self.assertEqual(self.service.payloads, [])

    def test_a_negative_content_length_is_refused(self) -> None:
        response = self._exchange(self._post("Content-Length: -1\r\n"))

        self.assertIn(b"400", response.split(b"\r\n", 1)[0])
        self.assertIn(b"invalid Content-Length", response)

    def test_a_garbage_content_length_is_a_400_not_a_500(self) -> None:
        response = self._exchange(self._post("Content-Length: lots\r\n"))

        self.assertIn(b"400", response.split(b"\r\n", 1)[0])

    def test_a_normal_request_still_reaches_the_service(self) -> None:
        body = json.dumps({"prompt": "p"}).encode()
        response = self._exchange(self._post(f"Content-Length: {len(body)}\r\n", body))

        self.assertIn(b"200", response.split(b"\r\n", 1)[0])
        self.assertEqual(self.service.payloads, [{"prompt": "p"}])

    def test_a_client_that_stalls_mid_body_is_timed_out(self) -> None:
        original = bridge.BridgeRequestHandler.timeout
        bridge.BridgeRequestHandler.timeout = 0.3
        self.addCleanup(setattr, bridge.BridgeRequestHandler, "timeout", original)

        # Promises 100 bytes, sends 5, then goes quiet. Without a timeout the handler
        # waits forever and this read times out in the test instead of getting a reply.
        response = self._exchange(self._post("Content-Length: 100\r\n", b"{...}"), read_timeout=3.0)

        self.assertIn(b"408", response.split(b"\r\n", 1)[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
