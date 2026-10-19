"""noVNC accepts a WebSocket only from its own page.

Chromium shares the browser-node network namespace with websockify, so any page
the browser loaded could open ws://127.0.0.1:6080/websockify and, with x11vnc's
default -nopw, read the screen and type into it. browser-node/novnc_origin.py
is the websockify auth plugin that refuses any Origin but the noVNC page's own.
"""

from __future__ import annotations

import http.client
import importlib.util
import sys
import tempfile
import threading
import types
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "browser-node" / "novnc_origin.py"
ENTRYPOINT = REPO_ROOT / "browser-node" / "entrypoint.sh"
DOCKERFILE = REPO_ROOT / "browser-node" / "Dockerfile"

# browser-node/ is not shipped in the controller image.
pytestmark = pytest.mark.skipif(not PLUGIN.is_file(), reason="browser-node/ is not shipped in the controller image")


def load_plugin():
    spec = importlib.util.spec_from_file_location("novnc_origin", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NoVncOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_plugin()

    def check(self, origin: str | None, host: str, source: str = "") -> bool:
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        headers["Host"] = host
        try:
            self.module.SameOriginOnly(source).authenticate(headers, "localhost", 5900)
        except self.module.InvalidOriginError:
            return False
        return True

    def test_the_novnc_page_itself_connects(self) -> None:
        self.assertTrue(self.check("http://127.0.0.1:6080", "127.0.0.1:6080"))
        self.assertTrue(self.check("http://localhost:6080", "localhost:6080"))
        self.assertTrue(self.check("http://[::1]:6080", "[::1]:6080"))
        self.assertTrue(self.check("https://takeover.example.com", "takeover.example.com", "takeover.example.com"))
        self.assertTrue(self.check("http://Test-Bastion:16080/", "test-bastion:16080", "test-bastion"))

    def test_dns_rebinding_to_loopback_is_refused(self) -> None:
        """Origin and Host agree on a rebound name; the name is not one we serve."""
        self.assertFalse(self.check("http://attacker.example:6080", "attacker.example:6080"))
        self.assertFalse(self.check("http://attacker.example:6080", "attacker.example:6080", "vnc.example.com"))
        self.assertFalse(self.check("http://localhost.attacker.example:6080", "localhost.attacker.example:6080"))

    def test_a_page_the_browser_visited_is_refused(self) -> None:
        self.assertFalse(self.check("https://evil.example", "127.0.0.1:6080"))
        self.assertFalse(self.check("http://127.0.0.1:8000", "127.0.0.1:6080"))
        self.assertFalse(self.check("null", "127.0.0.1:6080"))
        self.assertFalse(self.check("file://", "127.0.0.1:6080"))

    def test_a_handshake_without_origin_is_refused(self) -> None:
        self.assertFalse(self.check(None, "127.0.0.1:6080"))
        self.assertFalse(self.check("", "127.0.0.1:6080"))

    def test_listed_origins_are_admitted_for_host_rewriting_proxies(self) -> None:
        source = "https://x-6080.app.github.dev, https://vnc.example.com/"
        self.assertTrue(self.check("https://x-6080.app.github.dev", "localhost:6080", source))
        self.assertTrue(self.check("https://vnc.example.com", "localhost:6080", source))
        self.assertFalse(self.check("https://evil.example", "localhost:6080", source))
        # A listed origin's host is also a host the page may be opened on.
        self.assertTrue(self.check("https://vnc.example.com", "vnc.example.com", source))

    def test_the_image_runs_websockify_with_the_plugin(self) -> None:
        entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn("--auth-plugin novnc_origin.SameOriginOnly", entrypoint)
        self.assertNotIn("novnc_proxy", entrypoint)
        self.assertIn("COPY novnc_origin.py /opt/browser-node/novnc_origin.py", DOCKERFILE.read_text(encoding="utf-8"))


class NoVncFramingTests(unittest.TestCase):
    """The Origin check does not stop a page from framing vnc.html: the framed page's own
    WebSocket carries noVNC's Origin and passes. So websockify's HTTP responses forbid framing.

    websockify is not installed for this suite, so a stand-in module with the same class shape
    (WebSockifyRequestHandler is a SimpleHTTPRequestHandler) is served by a real HTTP server.
    """

    def serve(self):
        handler_class = type("WebSockifyRequestHandler", (SimpleHTTPRequestHandler,), {})
        modules = {
            "websockify": types.ModuleType("websockify"),
            "websockify.websockifyserver": types.ModuleType("websockify.websockifyserver"),
        }
        modules["websockify.websockifyserver"].WebSockifyRequestHandler = handler_class
        patcher = patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        plugin = load_plugin()  # websockify imports the plugin at startup; importing it installs the headers

        web = tempfile.TemporaryDirectory()
        self.addCleanup(web.cleanup)
        (Path(web.name) / "vnc.html").write_text("<html>vnc</html>", encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(handler_class, directory=web.name))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return plugin, server.server_address[1]

    def fetch(self, port: int, path: str):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", path)
        return connection.getresponse()

    def test_the_novnc_page_forbids_framing(self) -> None:
        _, port = self.serve()

        response = self.fetch(port, "/vnc.html")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(response.getheader("Content-Security-Policy"), "frame-ancestors 'none'")

    def test_error_responses_forbid_framing_too(self) -> None:
        _, port = self.serve()

        response = self.fetch(port, "/missing.html")

        self.assertEqual(response.status, 404)
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")

    def test_installing_twice_does_not_repeat_the_headers(self) -> None:
        plugin, port = self.serve()
        self.assertTrue(plugin.forbid_framing())

        response = self.fetch(port, "/vnc.html")

        self.assertEqual(len(response.msg.get_all("X-Frame-Options")), 1)

    def test_without_websockify_the_import_still_succeeds(self) -> None:
        self.assertFalse(load_plugin().forbid_framing())


if __name__ == "__main__":
    unittest.main()
