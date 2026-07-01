"""CI-safe tests for the local fixture server (no browser, no network egress)."""

from __future__ import annotations

import sys
import unittest
import urllib.request
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fixture_server import serve_fixtures  # noqa: E402


class FixtureServerTests(unittest.TestCase):
    def test_serves_html_and_data_fixtures(self) -> None:
        with serve_fixtures() as base_url:
            self.assertTrue(base_url.startswith("http://127.0.0.1:"))
            html = urllib.request.urlopen(f"{base_url}/multi_tab.html", timeout=5).read().decode("utf-8")
            self.assertIn("Primary workspace", html)
            # A non-HTML data fixture is served too.
            csv = urllib.request.urlopen(f"{base_url}/report.csv", timeout=5).read()
            self.assertTrue(csv)

    def test_missing_fixture_returns_404(self) -> None:
        with serve_fixtures() as base_url:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"{base_url}/does-not-exist.html", timeout=5)
            self.assertEqual(ctx.exception.code, 404)

    def test_missing_directory_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            with serve_fixtures("/nonexistent/fixtures/dir"):
                pass


if __name__ == "__main__":
    unittest.main()
