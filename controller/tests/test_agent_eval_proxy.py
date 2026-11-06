"""agent_eval's controller client sends a loopback controller's token past any environment proxy (W7-1).

urllib bypasses a proxy for localhost only when no_proxy names it, so with http_proxy set and no NO_PROXY
`scripts/agent_eval.py --execute` sent its Authorization header to the proxy. A loopback base URL is now reached
directly; any other host still goes through the environment's proxy.

The client runs in a child process whose environment names the proxy from the start, as an operator's shell would:
urllib.request.urlopen builds its opener, and reads the proxy variables, once per process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MARKER = "m" * 43
_IMPORT_ROOT = Path(__file__).resolve().parents[1]
_CHILD = """
import json, sys
from app.agent_eval import ControllerEvalClient
client = ControllerEvalClient(sys.argv[1], bearer_token=sys.argv[2])
print(json.dumps(client._post("/sessions", {})))
"""


class _Recording(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append((self.path, self.headers.get("Authorization")))  # type: ignore[attr-defined]
        body = b'{"id": "s1"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def _serve() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recording)
    server.seen = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class AgentEvalProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proxy, self.controller = _serve(), _serve()
        for server in (self.proxy, self.controller):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def _post(self, base_url: str) -> dict:
        proxy_url = f"http://127.0.0.1:{self.proxy.server_port}"
        env = {"http_proxy": proxy_url, "HTTP_PROXY": proxy_url, "PYTHONPATH": str(_IMPORT_ROOT)}
        for name in ("PATH", "SYSTEMROOT"):  # SYSTEMROOT: Python's sockets need it on Windows
            if name in os.environ:
                env[name] = os.environ[name]
        result = subprocess.run(
            [sys.executable, "-c", _CHILD, base_url, MARKER],
            env=env,
            cwd=_IMPORT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_a_loopback_controller_is_reached_directly(self) -> None:
        self.assertEqual(self._post(f"http://127.0.0.1:{self.controller.server_port}"), {"id": "s1"})

        self.assertEqual(self.proxy.seen, [])  # type: ignore[attr-defined]
        self.assertEqual(self.controller.seen, [("/sessions", f"Bearer {MARKER}")])  # type: ignore[attr-defined]

    def test_a_remote_controller_still_goes_through_the_proxy(self) -> None:
        # controller.invalid is never resolved: the request goes to the (local) recording proxy.
        self.assertEqual(self._post("http://controller.invalid:8000"), {"id": "s1"})

        self.assertEqual(self.proxy.seen, [("http://controller.invalid:8000/sessions", f"Bearer {MARKER}")])  # type: ignore[attr-defined]
        self.assertEqual(self.controller.seen, [])  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
