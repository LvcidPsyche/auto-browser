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
import socket
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


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _serve(host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = (_V6Server if ":" in host else ThreadingHTTPServer)((host, 0), _Recording)
    server.seen = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class AgentEvalProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        # Each server's cleanup registered as it starts (W12-7): a second bind that fails leaves no listener behind.
        self.proxy = self._started()
        self.controller = self._started()

    def _started(self) -> ThreadingHTTPServer:
        server = _serve()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

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

    def _post(self, base_url: str) -> dict:
        result = self._post_raw(base_url)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def _assert_the_direct_attempt_was_made(self, result: subprocess.CompletedProcess) -> None:
        """The child reached the controller directly, or failed in that direct attempt with urllib's URLError.

        Not a failure before any request is sent (W10-14's TypeError), which a check of the proxy alone passed."""
        self.assertEqual(self.proxy.seen, [])  # type: ignore[attr-defined]
        if result.returncode == 0:
            seen = self.controller.seen  # type: ignore[attr-defined]
            self.assertEqual(seen, [("/sessions", f"Bearer {MARKER}")])
        else:
            self.assertIn("urllib.error.URLError", result.stderr)

    def test_a_loopback_controller_is_reached_directly(self) -> None:
        self.assertEqual(self._post(f"http://127.0.0.1:{self.controller.server_port}"), {"id": "s1"})

        self.assertEqual(self.proxy.seen, [])  # type: ignore[attr-defined]
        self.assertEqual(self.controller.seen, [("/sessions", f"Bearer {MARKER}")])  # type: ignore[attr-defined]

    def test_w10_4_a_percent_encoded_loopback_host_is_reached_directly(self) -> None:
        # urllib connects to the host it has percent-decoded (127.0.0.1); judged as written it read as remote, and
        # the Authorization header went to the proxy.
        base_url = f"http://%31%32%37.0.0.1:{self.controller.server_port}"
        self.assertEqual(self._post(base_url), {"id": "s1"})

        self.assertEqual(self.proxy.seen, [])  # type: ignore[attr-defined]
        self.assertEqual(self.controller.seen, [("/sessions", f"Bearer {MARKER}")])  # type: ignore[attr-defined]

    def test_a_bind_all_or_trailing_dot_host_never_goes_through_the_proxy(self) -> None:
        # The direct request may fail where nothing answers on that address (0.0.0.0 on Windows, :: on a host
        # without IPv6); W11-8: either way it is the direct attempt's outcome, and the proxy saw nothing.
        for host in ("0.0.0.0", "[::]", "localhost."):
            with self.subTest(host=host):
                self.proxy.seen.clear()  # type: ignore[attr-defined]
                self.controller.seen.clear()  # type: ignore[attr-defined]
                self._assert_the_direct_attempt_was_made(self._post_raw(f"http://{host}:{self.controller.server_port}"))

    def test_w11_1_an_unbracketed_ipv6_loopback_is_reached_directly(self) -> None:
        # http.client reads the unbracketed ::1:PORT (and its percent-encoded spelling, which urllib decodes) as ::1,
        # while urlsplit read no host at all, so the Authorization header went to the proxy.
        try:
            controller = _serve("::1")
        except OSError:
            self.skipTest("no IPv6 loopback here")
        self.addCleanup(controller.server_close)
        self.addCleanup(controller.shutdown)
        for host in ("::1", "%3a%3a1"):
            with self.subTest(host=host):
                self.proxy.seen.clear()  # type: ignore[attr-defined]
                controller.seen.clear()  # type: ignore[attr-defined]
                self.assertEqual(self._post(f"http://{host}:{controller.server_port}"), {"id": "s1"})
                self.assertEqual(self.proxy.seen, [])  # type: ignore[attr-defined]
                self.assertEqual(controller.seen, [("/sessions", f"Bearer {MARKER}")])  # type: ignore[attr-defined]

    def test_w11_1_a_host_any_reading_finds_local_never_goes_through_the_proxy(self) -> None:
        # Each is local as the host http.client connects to. The direct attempt may fail (nothing listens on ::1 at
        # this port, and a host without IPv6 has no ::1); a failure is urllib's URLError. W13-2 refuses the W11-1
        # authorities whose connect host is not local.
        for authority in (
            "::1",
            "%3a%3a1",
            "::ffff:127.0.0.1",
            # W12-1: the socket layer's idna codec maps these (a soft hyphen, a masculine ordinal) to localhost.
            "local%C2%ADhost",
            "l%C2%BAcalhost",
        ):
            with self.subTest(authority=authority):
                self.proxy.seen.clear()  # type: ignore[attr-defined]
                self.controller.seen.clear()  # type: ignore[attr-defined]
                result = self._post_raw(f"http://{authority}:{self.controller.server_port}")
                self._assert_the_direct_attempt_was_made(result)

    def test_w13_2_a_host_local_one_way_but_connected_to_as_another_is_refused(self) -> None:
        # W12-2's portless IPv6 forms (http.client connects to ":" or "::ffff:7f00"), a userinfo before a portless
        # ::1 (which hid it from every reading, so the token went to the proxy), and the W11-1 authorities connected
        # to as 127.0.0.2#.bad, user:pass@127.0.0.1 and x/@127.0.0.1: the direct attempt went to a name the
        # resolver is asked for. Nothing is sent anywhere.
        port = self.controller.server_port
        for base_url in (
            "http://::1",
            "http://::",
            "http://%3a%3a1",
            "http://::ffff:7f00:1",
            "http://user@::1",
            "http://user:pass@::1",
            "http://user@%3a%3a1",
            # W14-4: with a port as well, until the host http.client reads after the last @ was a reading.
            "http://user@::1:8000",
            "http://user:pass@::1:8000",
            "http://user@%3a%3a1:8000",
            "http://user@::ffff:127.0.0.1:8000",
            f"http://127.0.0.2%23.bad:{port}",
            f"http://user:pass@127.0.0.1:{port}",
            f"http://x%2f@127.0.0.1:{port}",
        ):
            with self.subTest(base_url=base_url):
                self.proxy.seen.clear()  # type: ignore[attr-defined]
                result = self._post_raw(base_url)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("app.mcp_stdio.InvalidControllerURL", result.stderr)
                self.assertIn("reads as a local host", result.stderr)
                self.assertEqual(self.proxy.seen, [])  # type: ignore[attr-defined]
                self.assertEqual(self.controller.seen, [])  # type: ignore[attr-defined]

    def test_w12_3_a_malformed_authority_is_refused_as_an_invalid_url(self) -> None:
        # One reading's parse error (urlsplit's ValueError for the first two, http.client's InvalidURL for the port)
        # is raised as the bridges' InvalidControllerURL, a subclass of http.client's InvalidURL (W13-1), before any
        # opener is chosen.
        port = self.controller.server_port
        for authority in (f"%5b127.0.0.1%5d:{port}", f"x%5b@127.0.0.1:{port}", "controller.invalid:abc"):
            with self.subTest(authority=authority):
                result = self._post_raw(f"http://{authority}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("app.mcp_stdio.InvalidControllerURL", result.stderr)
                self.assertEqual(self.proxy.seen, [])  # type: ignore[attr-defined]
                self.assertEqual(self.controller.seen, [])  # type: ignore[attr-defined]

    def test_w11_1_a_non_numeric_port_is_refused_before_any_proxy_is_used(self) -> None:
        result = self._post_raw("http://controller.invalid:abc")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("InvalidURL", result.stderr)
        # W14-1: in the bridges' own words, chained to http.client's error for the operator.
        self.assertIn("app.mcp_stdio.InvalidControllerURL: http.client cannot read its host and port", result.stderr)
        self.assertEqual(self.proxy.seen, [])  # type: ignore[attr-defined]

    def test_w14_1_a_path_with_a_space_is_refused_as_an_invalid_url(self) -> None:
        # http.client refused it only once the direct connection was chosen; it is refused before any opener.
        result = self._post_raw(f"http://127.0.0.1:{self.controller.server_port}/a b")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("app.mcp_stdio.InvalidControllerURL: its path or query holds a space", result.stderr)
        self.assertEqual(self.proxy.seen, [])  # type: ignore[attr-defined]
        self.assertEqual(self.controller.seen, [])  # type: ignore[attr-defined]

    def test_w11_1_a_loopback_userinfo_before_a_remote_host_still_goes_through_the_proxy(self) -> None:
        # 127.0.0.1 is the userinfo here; the host is controller.invalid all three ways, so the proxy is used.
        self.assertEqual(self._post("http://127.0.0.1@controller.invalid:8000"), {"id": "s1"})

        self.assertEqual([auth for _, auth in self.proxy.seen], [f"Bearer {MARKER}"])  # type: ignore[attr-defined]
        self.assertEqual(self.controller.seen, [])  # type: ignore[attr-defined]

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
