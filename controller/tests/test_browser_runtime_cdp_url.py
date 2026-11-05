"""browser.cdp_attach logs, audits and returns scheme://host:port only (NAV-39).

A CDP URL can carry userinfo, a query token and a /devtools/browser/<id> path, and that path is itself the
credential for the Chrome it names.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.browser.services.runtime import scrubbed_cdp_url
from app.browser_manager import BrowserManager
from tests.test_browser_manager_create_session import _settings
from tests.test_cdp_attach import _browser

SECRET_URL = "ws://user:hunter2@10.0.0.9:9222/devtools/browser/abc-secret-id?token=tok-secret"


class ScrubbedCdpUrlTests(unittest.TestCase):
    def test_only_scheme_host_and_port_survive(self) -> None:
        for url, expected in (
            (SECRET_URL, "ws://10.0.0.9:9222"),
            ("http://localhost:9222/json/version", "http://localhost:9222"),
            ("https://chrome.example.com/?k=v#frag", "https://chrome.example.com"),
            ("ws://[::1]:9222/devtools/browser/x", "ws://[::1]:9222"),
            ("http://10.0.0.9:9222", "http://10.0.0.9:9222"),
        ):
            with self.subTest(url=url):
                self.assertEqual(scrubbed_cdp_url(url), expected)

    def test_an_unparseable_url_is_not_echoed(self) -> None:
        for url in ("not a url", "http://[bad", "http://host:notaport/x?secret=1"):
            with self.subTest(url=url):
                shown = scrubbed_cdp_url(url)
                self.assertNotIn("secret", shown)
                self.assertNotIn("notaport/x", shown)


class CdpAttachScrubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = BrowserManager(_settings(Path(self.tmp.name)))
        self.manager.settings.api_bearer_token = "only-token"
        self.attacker = _browser("attacker")
        self.manager.browser = None
        self.connect = AsyncMock(return_value=self.attacker)
        self.manager.playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=self.connect))
        self.audit = AsyncMock()
        self.manager.audit.append = self.audit

    async def test_log_audit_and_result_carry_no_credentials(self) -> None:
        with self.assertLogs("app.browser.services.runtime", level="INFO") as captured:
            result = await self.manager.cdp_attach(SECRET_URL)

        self.connect.assert_awaited_once_with(SECRET_URL)  # the browser is still reached at the full URL
        self.assertEqual(result["cdp_url"], "ws://10.0.0.9:9222")
        self.assertEqual(self.audit.await_args.kwargs["details"], {"cdp_url": "ws://10.0.0.9:9222"})
        shown = "\n".join(captured.output) + repr(result) + repr(self.audit.await_args)
        for leaked in ("hunter2", "abc-secret-id", "tok-secret", "user:"):
            self.assertNotIn(leaked, shown)


if __name__ == "__main__":
    unittest.main()
