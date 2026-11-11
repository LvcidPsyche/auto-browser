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
# http(s) URLs the gate finds no host name in, and what of them a caller would not want in a log.
NO_HOST_NAME = (
    "https://:hunter2@/path?token=SECRET-TOKEN-123",
    "http://alice:hunter2@:8080/path?token=SECRET-TOKEN-123",
    "HTTPS://:@/path?token=SECRET-TOKEN-123#hunter2",
)
PARTS_OF_A_URL_WITH_NO_HOST_NAME = ("hunter2", "SECRET-TOKEN-123", "alice", "path", "8080")


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

    def test_a_blob_url_is_judged_by_the_origin_that_made_it(self) -> None:
        """blob:https://host/uuid is a file that host's page generated; it passed on its scheme alone."""
        manager = BrowserManager(Settings(_env_file=None, ALLOWED_HOSTS="allowed.example"))
        with self.assertRaises(PermissionError):
            manager._assert_runtime_url_allowed("blob:https://refused.example/9b1d")
        manager._assert_runtime_url_allowed("blob:https://allowed.example/9b1d")
        # An opaque origin names no host; the page that made it is judged instead.
        manager._assert_runtime_url_allowed("blob:null/9b1d")

    def test_a_url_with_no_host_name_is_refused_in_words_that_repeat_none_of_it(self) -> None:
        # A refusal's text is logged by the routes and kept in records. Every other one names a host: this one held
        # the URL as it was sent, with its userinfo and its query.
        manager = BrowserManager(Settings(_env_file=None))
        for url in NO_HOST_NAME:
            with self.subTest(url=url):
                with self.assertRaises(PermissionError) as refused:
                    manager._assert_url_allowed(url)

                text = str(refused.exception)
                self.assertIn("no host name", text)
                for part in PARTS_OF_A_URL_WITH_NO_HOST_NAME:
                    self.assertNotIn(part, text)

    def test_a_refused_host_is_named_whole_up_to_200_characters_and_cut_past_them(self) -> None:
        # The host is the caller's text, and the fork route takes start_url as a query parameter no model bounds.
        manager = BrowserManager(Settings(_env_file=None, ALLOWED_HOSTS="allowed.example"))
        at_the_cut, past_it = "a" * 192 + ".example", "a" * 193 + ".example"
        said = {}
        for host in ("evil.example", at_the_cut, past_it, "a" * 3000 + ".example"):
            with self.assertRaises(PermissionError) as refused:
                manager._assert_url_allowed(f"https://{host}/x")
            said[host] = str(refused.exception)

        self.assertEqual(said["evil.example"], "Host 'evil.example' is not allowlisted")
        self.assertEqual(said[at_the_cut], f"Host '{at_the_cut}' is not allowlisted")
        self.assertEqual(said[past_it], f"Host '{past_it[:200]}...' is not allowlisted")
        self.assertEqual(said["a" * 3000 + ".example"], f"Host '{'a' * 200}...' is not allowlisted")


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

    def test_a_navigation_to_a_url_with_no_host_name_leaves_none_of_the_url_in_the_log(self) -> None:
        # The route logs the cause of its fixed 403: the gate's own text, which was the whole URL.
        async def navigate(_session_id: str, url: str) -> None:
            main_module.manager._assert_url_allowed(url)

        for url in NO_HOST_NAME:
            with self.subTest(url=url):
                with (
                    patch.object(main_module.manager, "navigate", navigate),
                    self.assertLogs("app.routes.sessions", level="WARNING") as logs,
                ):
                    response = self.client.post("/sessions/abc/actions/navigate", json={"url": url})

                self.assertEqual((response.status_code, response.json()), (403, {"detail": "Not permitted"}))
                logged = "\n".join(logs.output)
                self.assertIn("answered 403 for PermissionError", logged)
                for part in PARTS_OF_A_URL_WITH_NO_HOST_NAME:
                    self.assertNotIn(part, logged)


if __name__ == "__main__":
    unittest.main()
