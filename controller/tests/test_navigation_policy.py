"""NAVIGATION_POLICY=public_internet: any public site, never a private/internal one.

The owner's assistants must be able to open any site he names, so the tenant
can drop its per-host allowlist -- but the browser sits on a Docker network next
to the controller, the approval broker and the cloud metadata endpoint, so every
spelling of a private address, every internal name, every non-http(s) scheme and
every name whose DNS answer is private must still be refused.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app import navigation_policy
from app.browser_manager import BrowserManager
from app.config import Settings
from app.navigation_policy import (
    NavigationRefused,
    assert_resolves_public,
    check_public_url,
    whatwg_ipv4,
)
from app.runtime_policy import MIN_PRODUCTION_BEARER_TOKEN_LENGTH, validate_runtime_policy

PUBLIC_URLS = (
    "https://www.canva.com/",
    "http://example.com/path?q=1",
    "https://accounts.google.com/o/oauth2/v2/auth?client_id=x",
    "https://93.184.216.34/",
    "https://[2606:4700:4700::1111]/",
    "https://sub.domain.example.co.uk:8443/x",
)

REFUSED_URLS = (
    # schemes
    "file:///etc/passwd",
    "file://example.com/etc/passwd",
    "chrome://settings",
    "chrome-extension://abc/page.html",
    "data:text/html,<script>alert(1)</script>",
    "javascript:alert(1)",
    "view-source:https://example.com",
    "ws://example.com/socket",
    "ftp://example.com/file",
    "about:blank",
    # loopback / private / link-local / metadata, in every spelling Chromium accepts
    "http://localhost/",
    "http://LOCALHOST./",
    "http://app.localhost/",
    "http://127.0.0.1/",
    "http://127.1/",
    "http://2130706433/",
    "http://0x7f000001/",
    "http://0x7f.1/",
    "http://0177.0.0.1/",
    "http://%31%32%37.0.0.1/",
    "http://１２７.０.０.１/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:a9fe:a9fe]/",
    "http://[64:ff9b::a9fe:a9fe]/",
    "http://[fe80::1]/",
    "http://[fd00::1]/",
    "http://0.0.0.0/",
    "http://10.1.2.3/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://100.64.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://224.0.0.1/",
    "http://255.255.255.255/",
    # internal names: every Docker service on the tenant network is single-label
    "http://controller:8000/sessions",
    "http://browser-node:9225/",
    "http://approval-broker/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://printer.local/",
    "http://nas.home.arpa/",
    "http://router.lan/",
    # malformed numeric hosts are parse failures, not names
    "http://1.2.3.4.5/",
    "http://example.999/",
    # backslash confusion lands on the internal host Chromium really loads
    "http://169.254.169.254\\@example.com/latest/meta-data/",
)


class PublicUrlCheckTests(unittest.TestCase):
    def test_public_sites_are_allowed(self) -> None:
        for url in PUBLIC_URLS:
            with self.subTest(url=url):
                check_public_url(url)

    def test_private_internal_and_non_http_targets_are_refused(self) -> None:
        for url in REFUSED_URLS:
            with self.subTest(url=url), self.assertRaises(NavigationRefused):
                check_public_url(url)

    def test_refusal_is_a_permission_error_so_routes_answer_403(self) -> None:
        with self.assertRaises(PermissionError):
            check_public_url("http://127.0.0.1/")

    def test_denylist_blocks_a_host_and_its_subdomains_only(self) -> None:
        deny = "bad.example, *.tracker.test"
        for url in ("https://bad.example/", "https://www.bad.example/x", "https://a.tracker.test/"):
            with self.subTest(url=url), self.assertRaises(NavigationRefused):
                check_public_url(url, deny_hosts=deny)
        for url in ("https://notbad.example/", "https://tracker.test.evil.com/"):
            with self.subTest(url=url):
                check_public_url(url, deny_hosts=deny)

    def test_whatwg_ipv4_reads_what_chromium_reads(self) -> None:
        self.assertEqual(str(whatwg_ipv4("2130706433")), "127.0.0.1")
        self.assertEqual(str(whatwg_ipv4("0x7f.1")), "127.0.0.1")
        self.assertEqual(str(whatwg_ipv4("127.1")), "127.0.0.1")
        self.assertEqual(str(whatwg_ipv4("0177.0.0.1")), "127.0.0.1")
        self.assertEqual(str(whatwg_ipv4("1.2.3")), "1.2.0.3")
        self.assertIsNone(whatwg_ipv4("example.com"))
        with self.assertRaises(ValueError):
            whatwg_ipv4("256.1.1.1")


class DnsRebindingGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        navigation_policy.clear_dns_cache()
        self.addCleanup(navigation_policy.clear_dns_cache)
        self._original = navigation_policy._resolve
        self.addCleanup(setattr, navigation_policy, "_resolve", self._original)

    def _answer(self, *addresses: str) -> None:
        async def fake(host: str) -> list[str]:
            return list(addresses)

        navigation_policy._resolve = fake

    def test_public_answer_passes(self) -> None:
        self._answer("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")
        asyncio.run(assert_resolves_public("https://example.com/"))

    def test_a_public_name_resolving_to_metadata_is_refused(self) -> None:
        self._answer("169.254.169.254")
        with self.assertRaises(NavigationRefused):
            asyncio.run(assert_resolves_public("https://169.254.169.254.nip.io/latest"))

    def test_one_private_address_in_the_answer_is_enough_to_refuse(self) -> None:
        self._answer("93.184.216.34", "10.0.0.7")
        with self.assertRaises(NavigationRefused):
            asyncio.run(assert_resolves_public("https://rebind.example/"))

    def test_lookup_failure_fails_closed(self) -> None:
        async def broken(host: str) -> list[str]:
            raise OSError("no such host")

        navigation_policy._resolve = broken
        with self.assertRaises(NavigationRefused):
            asyncio.run(assert_resolves_public("https://nx.example/"))

    def test_literal_public_ip_needs_no_lookup(self) -> None:
        async def must_not_run(host: str) -> list[str]:
            raise AssertionError("literal addresses are not looked up")

        navigation_policy._resolve = must_not_run
        asyncio.run(assert_resolves_public("https://93.184.216.34/"))


def _manager(root: Path, **env: str) -> BrowserManager:
    return BrowserManager(
        Settings(
            _env_file=None,
            ARTIFACT_ROOT=str(root / "artifacts"),
            UPLOAD_ROOT=str(root / "uploads"),
            AUTH_ROOT=str(root / "auth"),
            APPROVAL_ROOT=str(root / "approvals"),
            AUDIT_ROOT=str(root / "audit"),
            SESSION_STORE_ROOT=str(root / "sessions"),
            **env,
        )
    )


class ManagerNavigationModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        navigation_policy.clear_dns_cache()
        self.addCleanup(navigation_policy.clear_dns_cache)

    def test_default_allowlist_mode_is_unchanged(self) -> None:
        manager = _manager(Path(self.tmp.name), ALLOWED_HOSTS="example.com")
        manager._assert_url_allowed("https://example.com/")
        with self.assertRaises(PermissionError):
            manager._assert_url_allowed("https://www.canva.com/")
        # And no DNS check in allowlist mode: the owner named every host himself.
        asyncio.run(manager._assert_url_resolves_public("https://example.com/"))

    def test_public_mode_opens_any_public_site_but_never_internal(self) -> None:
        manager = _manager(
            Path(self.tmp.name),
            ALLOWED_HOSTS="example.com",
            NAVIGATION_POLICY="public_internet",
            NAVIGATION_DENY_HOSTS="blocked.example",
        )
        manager._assert_url_allowed("https://www.canva.com/")
        manager._assert_url_allowed("https://accounts.google.com/signin")
        for url in (
            "http://controller:8000/",
            "http://169.254.169.254/",
            "file:///etc/passwd",
            "https://blocked.example/",
        ):
            with self.subTest(url=url), self.assertRaises(PermissionError):
                manager._assert_url_allowed(url)

    def test_public_mode_runtime_check_refuses_a_redirect_to_a_private_host(self) -> None:
        manager = _manager(Path(self.tmp.name), NAVIGATION_POLICY="public_internet")
        manager._assert_runtime_url_allowed("about:blank")
        manager._assert_runtime_url_allowed("chrome-error://chromewebdata/")
        with self.assertRaises(PermissionError):
            manager._assert_runtime_url_allowed("http://10.0.0.5/admin")

        async def private(host: str) -> list[str]:
            return ["192.168.0.10"]

        original = navigation_policy._resolve
        navigation_policy._resolve = private
        try:
            with self.assertRaises(PermissionError):
                asyncio.run(manager._assert_runtime_url_resolves_public("https://rebound.example/"))
        finally:
            navigation_policy._resolve = original


class ProductionPolicyTests(unittest.TestCase):
    def test_public_mode_is_an_explicit_production_choice_not_an_error(self) -> None:
        settings = Settings(
            _env_file=None,
            APP_ENV="production",
            API_BEARER_TOKEN="p" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH,
            SHARE_TOKEN_SECRET="share-secret-for-tests",
            REQUIRE_OPERATOR_ID="true",
            AUTH_STATE_ENCRYPTION_KEY="b" * 44,
            REQUIRE_AUTH_STATE_ENCRYPTION="true",
            ALLOWED_HOSTS="__deny_all__.invalid",
            CONTROLLER_ALLOWED_HOSTS="controller.example.com",
            NAVIGATION_POLICY="public_internet",
        )
        report = validate_runtime_policy(settings)
        self.assertFalse(any("ALLOWED_HOSTS" in error for error in report.errors), report.errors)
        self.assertTrue(any("NAVIGATION_POLICY=public_internet" in w for w in report.warnings))

    def test_a_bare_star_is_still_refused_in_production(self) -> None:
        settings = Settings(
            _env_file=None,
            APP_ENV="production",
            API_BEARER_TOKEN="p" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH,
            ALLOWED_HOSTS="*",
            CONTROLLER_ALLOWED_HOSTS="controller.example.com",
        )
        report = validate_runtime_policy(settings)
        self.assertIn("ALLOWED_HOSTS=* is not permitted when APP_ENV=production", report.errors)


if __name__ == "__main__":
    unittest.main()
