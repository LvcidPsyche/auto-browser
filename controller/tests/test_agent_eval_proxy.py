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
        redirect_to = getattr(self.server, "redirect_to", None)
        if redirect_to:
            self.send_response(302)
            self.send_header("Location", redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"id": "s1"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST  # noqa: N815 - a followed 302 turns the POST into a GET

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

    def _post_raw(self, base_url: str) -> subprocess.CompletedProcess:
        proxy_url = f"http://127.0.0.1:{self.proxy.server_port}"
        env = {"http_proxy": proxy_url, "HTTP_PROXY": proxy_url, "PYTHONPATH": str(_IMPORT_ROOT)}
        for name in ("PATH", "SYSTEMROOT"):  # SYSTEMROOT: Python's sockets need it on Windows
            if name in os.environ:
                env[name] = os.environ[name]
        return subprocess.run(
            [sys.executable, "-c", _CHILD, base_url, MARKER],
            env=env,
            cwd=_IMPORT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_w9_6_a_redirect_is_reported_and_not_followed(self) -> None:
        # urllib's default handler copied the Authorization header onto a redirect to any origin.
        elsewhere = _serve()
        self.addCleanup(elsewhere.server_close)
        self.addCleanup(elsewhere.shutdown)
        target = f"http://127.0.0.1:{elsewhere.server_port}/stolen"
        for base_url, server in (
            (f"http://127.0.0.1:{self.controller.server_port}", self.controller),
            ("http://controller.invalid:8000", self.proxy),
        ):
            with self.subTest(base_url=base_url):
                server.seen.clear()  # type: ignore[attr-defined]
                server.redirect_to = target  # type: ignore[attr-defined]
                result = self._post_raw(base_url)
                server.redirect_to = None  # type: ignore[attr-defined]
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("controller request failed: 302", result.stderr)
                # On the proxied branch a followed redirect to 127.0.0.1 goes through the proxy again.
                self.assertEqual(len(server.seen), 1, server.seen)  # type: ignore[attr-defined]
                self.assertEqual(elsewhere.seen, [])  # type: ignore[attr-defined]

    def _post(self, base_url: str, *, check: bool = True) -> dict | None:
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
        if not check:
            return None
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_a_loopback_controller_is_reached_directly(self) -> None:
        self.assertEqual(self._post(f"http://127.0.0.1:{self.controller.server_port}"), {"id": "s1"})

        self.assertEqual(self.proxy.seen, [])  # type: ignore[attr-defined]
        self.assertEqual(self.controller.seen, [("/sessions", f"Bearer {MARKER}")])  # type: ignore[attr-defined]

    def test_a_bind_all_or_trailing_dot_host_never_goes_through_the_proxy(self) -> None:
        # The direct request may fail where nothing answers on that address (0.0.0.0 on Windows, :: on a host
        # without IPv6); the assertion is what the proxy saw, which holds everywhere.
        for host in ("0.0.0.0", "[::]", "localhost."):
            with self.subTest(host=host):
                self._post(f"http://{host}:{self.controller.server_port}", check=False)
                self.assertEqual(self.proxy.seen, [])  # type: ignore[attr-defined]

    def test_a_remote_controller_still_goes_through_the_proxy(self) -> None:
        # controller.invalid is never resolved: the request goes to the (local) recording proxy.
        self.assertEqual(self._post("http://controller.invalid:8000"), {"id": "s1"})

        self.assertEqual(
            self.proxy.seen,  # type: ignore[attr-defined]
            [("http://controller.invalid:8000/sessions", f"Bearer {MARKER}")],
        )
        self.assertEqual(self.controller.seen, [])  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
