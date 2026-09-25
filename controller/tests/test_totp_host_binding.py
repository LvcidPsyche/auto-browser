"""One-time codes are typed only on the hosts the caller named.

The TOTP autofill runs after every action and types a live code into any
visible field that looks like a code input. It had no idea which site it was
on, so any page the agent reached (a link, a redirect, a search result) could
show `<input name="code">` and receive a valid second factor for the account.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import ValidationError

from app.browser.services.actions import BrowserActionService
from app.models import CreateSessionRequest, resolve_totp_hosts, totp_host_allowed

SECRET = "JBSWY3DPEHPK3PXP"


class TotpHostResolutionTests(unittest.TestCase):
    def test_defaults_to_the_start_url_host(self) -> None:
        request = CreateSessionRequest(start_url="https://Login.Example.com/signin", totp_secret=SECRET)
        self.assertEqual(request.totp_hosts, ["login.example.com"])

    def test_explicit_hosts_win_and_are_normalized(self) -> None:
        request = CreateSessionRequest(
            start_url="https://example.com",
            totp_secret=SECRET,
            totp_hosts=["*.Example.com.", "accounts.idp.test"],
        )
        self.assertEqual(request.totp_hosts, ["*.example.com", "accounts.idp.test"])

    def test_a_secret_with_nowhere_to_type_it_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            CreateSessionRequest(totp_secret=SECRET)
        with self.assertRaises(ValueError):
            resolve_totp_hosts(None, None)

    def test_hosts_without_a_secret_are_refused(self) -> None:
        with self.assertRaises(ValidationError):
            CreateSessionRequest(start_url="https://example.com", totp_hosts=["example.com"])

    def test_entries_must_be_host_names(self) -> None:
        for bad in ("https://example.com", "example.com/login", "*", "*.", "exa mple.com", "a.*.com"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resolve_totp_hosts([bad], None)

    def test_matching(self) -> None:
        self.assertTrue(totp_host_allowed("example.com", ["example.com"]))
        self.assertTrue(totp_host_allowed("EXAMPLE.com.", ["example.com"]))
        self.assertFalse(totp_host_allowed("login.example.com", ["example.com"]))
        self.assertTrue(totp_host_allowed("login.example.com", ["*.example.com"]))
        self.assertTrue(totp_host_allowed("example.com", ["*.example.com"]))
        self.assertFalse(totp_host_allowed("evilexample.com", ["*.example.com"]))
        self.assertFalse(totp_host_allowed("example.com.evil.test", ["example.com", "*.example.com"]))
        self.assertFalse(totp_host_allowed("example.com", []))


class TotpAutofillTests(unittest.TestCase):
    def run_autofill(self, url: str, *, hosts: tuple[str, ...]) -> tuple[object, AsyncMock]:
        typed = AsyncMock()
        locator = SimpleNamespace(fill=AsyncMock())
        service = BrowserActionService(SimpleNamespace(_settle=AsyncMock()))
        service.first_visible_locator = AsyncMock(side_effect=[(locator, 'input[name*="code" i]'), None])
        service.focus_locator = AsyncMock()
        service.type_text_human_like = typed
        session = SimpleNamespace(page=SimpleNamespace(url=url), totp_secret=SECRET, totp_hosts=hosts)
        return asyncio.run(service.maybe_handle_totp(session)), typed

    def test_types_the_code_on_a_named_host(self) -> None:
        result, typed = self.run_autofill("https://login.example.com/2fa", hosts=("login.example.com",))
        self.assertEqual(result["code_length"], 6)
        typed.assert_awaited_once()

    def test_a_page_on_another_host_gets_nothing(self) -> None:
        for url in (
            "https://evil.test/win-a-prize",
            "https://login.example.com.evil.test/",
            "data:text/html,<input name=code>",
            "about:blank",
        ):
            with self.subTest(url=url):
                result, typed = self.run_autofill(url, hosts=("login.example.com",))
                self.assertIsNone(result)
                typed.assert_not_awaited()

    def test_a_session_without_hosts_types_nowhere(self) -> None:
        """Sessions built without going through create() fail closed."""
        result, typed = self.run_autofill("https://login.example.com/2fa", hosts=())
        self.assertIsNone(result)
        typed.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
