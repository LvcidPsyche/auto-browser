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
from unittest.mock import AsyncMock, call, patch

from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from app.action_errors import BrowserActionError
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
    def run_autofill(
        self,
        url: str,
        *,
        hosts: tuple[str, ...],
        refused: bool = False,
        shown: str | None = None,
        leaves_during: str | None = None,
        fill_fails: bool = False,
        focused: bool = True,
    ) -> tuple[object, AsyncMock]:
        """``leaves_during``: the step ("search" for the field, "focus" for its click, "clear" for the field's
        fill(""), "clear by keys" for the Control+A and Delete made where that fill fails, as ``fill_fails`` has it,
        "typing" for the code's keystrokes) during which the page goes to another host. ``focused``: what the field
        answers when it is asked whether focus is in it."""
        typed = AsyncMock(side_effect=lambda *_args: leave_during("typing"))

        async def clear(_value: str) -> None:
            leave_during("clear")
            if fill_fails:
                raise PlaywrightError("Element is not an <input>, <textarea> or [contenteditable] element")

        async def press_key(_key: str) -> None:
            leave_during("clear by keys")

        locator = SimpleNamespace(
            fill=AsyncMock(side_effect=clear), press=AsyncMock(), evaluate=AsyncMock(return_value=focused)
        )
        manager = SimpleNamespace(
            _settle=AsyncMock(),
            _page_refused=lambda page: refused,
            _shown_url=lambda page: shown if shown is not None else page.url,
        )
        service = BrowserActionService(manager)
        keyboard = SimpleNamespace(press=AsyncMock(side_effect=press_key))
        session = SimpleNamespace(
            page=SimpleNamespace(url=url, keyboard=keyboard), totp_secret=SECRET, totp_hosts=hosts
        )

        def leave_during(step: str) -> None:
            if leaves_during == step:
                session.page.url = "https://evil.test/collect"

        async def find_the_field(_page: object) -> tuple[object, str]:
            leave_during("search")
            return locator, 'input[name*="code" i]'

        async def focus(*_args: object, **_named: object) -> None:
            self.focus_clicks += 1
            leave_during("focus")

        service._find_one_time_code_field = find_the_field
        service.focus_verified = focus
        service.type_text_human_like = typed
        self.pressed, self.focus_clicks, self.keys, self.cleared = locator.press, 0, keyboard.press, locator.fill
        return asyncio.run(service.maybe_handle_totp(session)), typed

    def test_types_the_code_on_a_named_host(self) -> None:
        result, typed = self.run_autofill("https://login.example.com/2fa", hosts=("login.example.com",))
        self.assertEqual(result["code_length"], 6)
        # A key at a time, the host checked before each.
        self.assertEqual([len(call.args[1]) for call in typed.await_args_list], [1] * 6)

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

    def test_a_refused_page_on_a_named_host_gets_nothing(self) -> None:
        """CR-4: the check before the autofill is by URL only, so a page a refused host's redirect sent to a
        named sign-in URL (with its own query) was given a live code."""
        result, typed = self.run_autofill(
            "https://login.example.com/2fa?d=X", hosts=("login.example.com",), refused=True
        )
        self.assertIsNone(result)
        typed.assert_not_awaited()

    def test_a_page_that_leaves_the_named_host_while_its_field_is_looked_for_gets_nothing(self) -> None:
        # Looking for the field awaits the page: no test held the check made after it, which keeps the focus click
        # off the other host's page as well.
        result, typed = self.run_autofill(
            "https://login.example.com/2fa", hosts=("login.example.com",), leaves_during="search"
        )
        self.assertIsNone(result)
        typed.assert_not_awaited()
        self.assertEqual(self.focus_clicks, 0)

    def test_a_page_that_leaves_the_named_host_during_the_focus_click_gets_nothing(self) -> None:
        # The focus click (the pointer's moves, a pause, the press) awaits the page as well, and nothing was checked
        # after it: a page the press sent to another host was given the live code, and Enter. The checks after the
        # clear and the typing keep the code and Enter off it too, so only the field's clear, by its fill("") or by
        # Control+A and Delete where that fails, shows whether the check after the click held.
        for fill_fails in (False, True):
            with self.subTest(fill_fails=fill_fails):
                result, typed = self.run_autofill(
                    "https://login.example.com/2fa",
                    hosts=("login.example.com",),
                    leaves_during="focus",
                    fill_fails=fill_fails,
                )
                self.assertIsNone(result)
                typed.assert_not_awaited()
                self.pressed.assert_not_awaited()
                self.cleared.assert_not_awaited()
                self.keys.assert_not_awaited()

    def test_a_page_that_leaves_the_named_host_as_its_field_is_cleared_gets_nothing(self) -> None:
        # Emptying the field (its fill(""), or where that fails Control+A and Delete) awaits the page as well, after
        # the last check: a field whose page sent it to another host as it was emptied (an input handler, a key
        # handler) had the live code typed into that host's page, and Enter.
        for step, fill_fails in (("clear", False), ("clear by keys", True)):
            with self.subTest(step=step):
                result, typed = self.run_autofill(
                    "https://login.example.com/2fa",
                    hosts=("login.example.com",),
                    leaves_during=step,
                    fill_fails=fill_fails,
                )
                self.assertIsNone(result)
                typed.assert_not_awaited()
                self.pressed.assert_not_awaited()

    def test_a_page_that_leaves_the_named_host_as_the_code_is_typed_gets_no_enter(self) -> None:
        # Typing the code awaits the page at each keystroke, after the last check: a page a key handler sent to
        # another host meanwhile had Enter pressed into it, which submits whatever form has the focus there.
        result, typed = self.run_autofill(
            "https://login.example.com/2fa", hosts=("login.example.com",), leaves_during="typing"
        )
        self.pressed.assert_not_awaited()
        self.assertIsNone(result)
        typed.assert_awaited_once()

    def test_a_field_emptied_by_keys_on_the_named_host_gets_the_code(self) -> None:
        # Where its fill fails the field is emptied by keys, and on the named host the code is typed as before.
        result, typed = self.run_autofill(
            "https://login.example.com/2fa", hosts=("login.example.com",), fill_fails=True
        )
        self.assertEqual(result["code_length"], 6)
        self.assertEqual(len(typed.await_args_list), 6)
        self.assertEqual(self.keys.await_args_list, [call("Control+a"), call("Delete")])

    def test_a_field_that_lost_focus_when_its_fill_failed_gets_no_keys(self) -> None:
        # The keys that empty a field whose fill failed go to whatever has focus: into another field of the page,
        # which the Delete emptied and the code went into. They are pressed only where focus is in the field.
        result, typed = self.run_autofill(
            "https://login.example.com/2fa", hosts=("login.example.com",), fill_fails=True, focused=False
        )
        self.assertIsNone(result)
        self.keys.assert_not_awaited()
        typed.assert_not_awaited()

    def test_totp_unavailable_names_the_page_as_it_is_shown(self) -> None:
        with patch("app.browser.services.actions.pyotp", None), self.assertRaises(BrowserActionError) as raised:
            self.run_autofill("https://login.example.com/2fa?d=X", hosts=("login.example.com",), shown="SHOWN-URL")
        self.assertEqual(raised.exception.code, "totp_unavailable")
        self.assertEqual(raised.exception.details["url"], "SHOWN-URL")

    def test_a_session_without_hosts_types_nowhere(self) -> None:
        """Sessions built without going through create() fail closed."""
        result, typed = self.run_autofill("https://login.example.com/2fa", hosts=())
        self.assertIsNone(result)
        typed.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
