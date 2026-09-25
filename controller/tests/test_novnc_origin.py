"""noVNC accepts a WebSocket only from its own page.

Chromium shares the browser-node network namespace with websockify, so any page
the browser loaded could open ws://127.0.0.1:6080/websockify and, with x11vnc's
default -nopw, read the screen and type into it. browser-node/novnc_origin.py
is the websockify auth plugin that refuses any Origin but the noVNC page's own.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

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
        self.assertTrue(self.check("https://takeover.example.com", "takeover.example.com"))
        self.assertTrue(self.check("http://Test-Bastion:16080/", "test-bastion:16080"))

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

    def test_the_image_runs_websockify_with_the_plugin(self) -> None:
        entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn("--auth-plugin novnc_origin.SameOriginOnly", entrypoint)
        self.assertNotIn("novnc_proxy", entrypoint)
        self.assertIn("COPY novnc_origin.py /opt/browser-node/novnc_origin.py", DOCKERFILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
