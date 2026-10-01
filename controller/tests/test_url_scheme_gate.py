"""The navigation gate decides the scheme too, not only the host.

`_assert_url_allowed` checked nothing but the hostname, and the scheme was left to
whichever request model the caller happened to use. `POST /sessions/{id}/fork`
takes `start_url` as a bare query parameter, so `file://localhost/...` reached the
gate, passed it (localhost is in the default ALLOWED_HOSTS), and Chromium opened
local files from the browser container: every session's downloads and the
browser profile are mounted there.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import main as main_module
from app.browser_manager import BrowserManager
from app.config import Settings

REFUSED = (
    "file://localhost/etc/passwd",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "data:text/html,<h1>x</h1>",
    "chrome://settings",
    "view-source:http://example.com/",
    "ftp://example.com/file",
)


class SchemeGateTests(unittest.TestCase):
    def test_only_http_and_https_pass_the_gate(self) -> None:
        manager = BrowserManager(Settings(_env_file=None))
        for url in REFUSED:
            with self.subTest(url=url), self.assertRaises(PermissionError):
                manager._assert_url_allowed(url)
        for url in ("http://example.com/x", "https://localhost:8443/", "HTTPS://EXAMPLE.COM/"):
            with self.subTest(url=url):
                manager._assert_url_allowed(url)

    def test_a_wildcard_allowlist_does_not_admit_other_schemes(self) -> None:
        manager = BrowserManager(Settings(_env_file=None, ALLOWED_HOSTS="*"))
        with self.assertRaises(PermissionError):
            manager._assert_url_allowed("file://localhost/etc/passwd")
        manager._assert_url_allowed("https://anything.example/")

    def test_the_runtime_check_still_tolerates_browser_internal_pages(self) -> None:
        manager = BrowserManager(Settings(_env_file=None, ALLOWED_HOSTS="*"))
        for url in ("about:blank", "chrome-error://chromewebdata/", "data:text/html,x", "blob:https://x/1"):
            with self.subTest(url=url):
                manager._assert_runtime_url_allowed(url)
        with self.assertRaises(PermissionError):
            manager._assert_runtime_url_allowed("file://localhost/etc/passwd")


class ForkRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[]))
        )
        for service in (main_module.manager, main_module.job_queue, main_module.cron_service, main_module.maintenance):
            for method_name in ("startup", "shutdown"):
                self.stack.enter_context(patch.object(service, method_name, new=AsyncMock()))
        self.client = self.stack.enter_context(TestClient(main_module.app))

    def tearDown(self) -> None:
        self.stack.close()

    def test_fork_refuses_a_non_http_start_url(self) -> None:
        fork = AsyncMock(return_value={"id": "forked"})
        with patch.object(main_module.manager, "fork_session", fork):
            response = self.client.post("/sessions/abc/fork", params={"start_url": "file://localhost/etc/passwd"})
        self.assertEqual(response.status_code, 400)
        fork.assert_not_awaited()

    def test_fork_reports_a_refused_host_as_403(self) -> None:
        fork = AsyncMock(side_effect=PermissionError("Host 'evil.example' is not allowlisted"))
        with patch.object(main_module.manager, "fork_session", fork):
            response = self.client.post("/sessions/abc/fork", params={"start_url": "https://evil.example/"})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
