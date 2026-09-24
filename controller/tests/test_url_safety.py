"""The navigation allowlist must judge the host the browser will actually load.

Python's urllib and Chromium's WHATWG parser disagree on backslashes: for
``http://evil.com\\@example.com/`` urllib reports ``example.com`` (all before
the last "@" is userinfo to it) while Chromium loads ``evil.com`` with path
``/@example.com/``. The allowlist trusted urllib, so appending
``\\@<allowlisted-host>`` to any URL got it through navigate, create_session
and open_tab. A differential fuzz of ~31k generated URLs against Chromium's own
``new URL()`` found 28 such bypasses before the fix and none after.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from app.browser_manager import BrowserManager
from app.config import Settings
from app.mesh.models import CapabilityGrant, DelegationRequest, PeerRecord
from app.mesh.policy import PolicyDenied, PolicyEvaluator
from app.url_safety import browser_equivalent_url

# (input, host Chromium loads) — each verified against Chromium's URL parser.
BROWSER_HOSTS = [
    ("http://evil.com\\@example.com/", "evil.com"),
    ("http://evil.com\\.example.com/", "evil.com"),
    ("https://evil.com\\\\@example.com/x", "evil.com"),
    (" \thttp://evil.com\\@example.com/ ", "evil.com"),
    ("HTTP://evil.com\\@example.com", "evil.com"),
    ("https://example.com/search?q=a\\b", "example.com"),
    ("https://example.com/p#frag\\@evil.com", "example.com"),
    ("http://evil.com;@example.com/", "example.com"),
]


class BrowserEquivalentUrlTests(unittest.TestCase):
    def test_urllib_reads_the_host_the_browser_loads(self) -> None:
        for url, browser_host in BROWSER_HOSTS:
            with self.subTest(url=url):
                self.assertEqual(urlparse(browser_equivalent_url(url)).hostname, browser_host)

    def test_backslashes_in_query_and_fragment_are_kept(self) -> None:
        self.assertEqual(
            browser_equivalent_url("https://example.com/a\\b?q=c\\d#e\\f"),
            "https://example.com/a/b?q=c\\d#e\\f",
        )

    def test_non_special_schemes_are_left_alone(self) -> None:
        self.assertEqual(browser_equivalent_url("mailto:a\\b"), "mailto:a\\b")


class NavigationAllowlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.manager = BrowserManager(
            Settings(
                _env_file=None,
                ALLOWED_HOSTS="example.com",
                ARTIFACT_ROOT=str(root / "artifacts"),
                UPLOAD_ROOT=str(root / "uploads"),
                AUTH_ROOT=str(root / "auth"),
                APPROVAL_ROOT=str(root / "approvals"),
                AUDIT_ROOT=str(root / "audit"),
                SESSION_STORE_ROOT=str(root / "sessions"),
            )
        )

    def test_backslash_host_confusion_is_refused(self) -> None:
        for url in ("http://evil.com\\@example.com/", "http://169.254.169.254\\@example.com/latest/meta-data/"):
            with self.subTest(url=url), self.assertRaises(PermissionError):
                self.manager._assert_url_allowed(url)

    def test_allowlisted_urls_still_pass(self) -> None:
        for url in ("https://example.com/", "https://app.example.com/x", "https://example.com/search?q=a\\b"):
            with self.subTest(url=url):
                self.manager._assert_url_allowed(url)


class MeshAllowlistBackslashTests(unittest.TestCase):
    def test_a_wildcard_host_pattern_is_not_fooled_by_a_backslash(self) -> None:
        peer = PeerRecord(
            node_id="peer1",
            pubkey_b64="A" * 44,
            grants=[CapabilityGrant(capability="tool:*", url_allowlist=["https://*.example.com/*"])],
        )
        request = DelegationRequest(
            capability="tool:browser.create_session", arguments={"url": "https://evil.com\\.example.com/"}
        )

        with self.assertRaises(PolicyDenied):
            PolicyEvaluator().evaluate(peer, request)


if __name__ == "__main__":
    unittest.main()
