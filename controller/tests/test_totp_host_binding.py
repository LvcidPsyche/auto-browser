"""One-time codes are typed only on the hosts the caller named.

The TOTP autofill runs after every action and types a live code into any
visible field that looks like a code input. It had no idea which site it was
on, so any page the agent reached (a link, a redirect, a search result) could
show `<input name="code">` and receive a valid second factor for the account.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from app.action_errors import BrowserActionError
from app.browser.element_scripts import ELEMENT_HAS_FOCUS_SCRIPT
from app.browser.services.actions import BrowserActionService
from app.models import CreateSessionRequest, resolve_totp_hosts, totp_host_allowed

SECRET = "JBSWY3DPEHPK3PXP"
# The code the success tests freeze, so that the keys typed are compared with it, in order.
CODE = "492039"


def _frozen_code() -> object:
    """pyotp, as the autofill uses it, answering CODE."""
    return patch(
        "app.browser.services.actions.pyotp", SimpleNamespace(TOTP=lambda _secret: SimpleNamespace(now=lambda: CODE))
    )


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
        focused: bool | list[bool] = True,
        keys_take: float = 0.0,
    ) -> tuple[object, AsyncMock]:
        """``leaves_during``: the step ("search" for the field, "focus" for its click, "clear" for the field's
        fill(""), "clear by keys" for the Control+A and Delete made where that fill fails, as ``fill_fails`` has it,
        "focus question" for the question whether focus is in the field, asked before each of them, "typing" for the
        code's keystrokes) during which the page goes to another host. ``focused``: what the field
        answers when it is asked whether focus is in it, or a list of its answers in turn. ``keys_take``: how many
        seconds the page takes to acknowledge each key of the clear by keys."""
        self.typed_keys: list[str] = []

        async def type_key(key: str) -> None:
            self.typed_keys.append(key)
            leave_during("typing")

        async def clear(_value: str) -> None:
            leave_during("clear")
            if fill_fails:
                raise PlaywrightError("Element is not an <input>, <textarea> or [contenteditable] element")

        async def press_key(_key: str) -> None:
            await asyncio.sleep(keys_take)
            leave_during("clear by keys")

        answers = list(focused) if isinstance(focused, list) else None

        async def asked(script: str, *_args: object, **_named: object) -> bool:
            # The field answers the focus check's own script only (B1-fix1 item 29, staged F13).
            assert script == ELEMENT_HAS_FOCUS_SCRIPT, script
            leave_during("focus question")
            return answers.pop(0) if answers is not None else focused

        locator = SimpleNamespace(
            fill=AsyncMock(side_effect=clear), press=AsyncMock(), evaluate=AsyncMock(side_effect=asked)
        )
        manager = SimpleNamespace(
            settings=SimpleNamespace(human_typing_min_delay_ms=0, human_typing_max_delay_ms=0),
            _settle=AsyncMock(),
            _page_refused=lambda page: refused,
            _shown_url=lambda page: shown if shown is not None else page.url,
        )
        service = BrowserActionService(manager)
        # The typing code itself runs, with no pauses, over a keyboard that notes each key (B1-fix2 item 16): a copy
        # of its loop here passed a typing code that asked the host after each key, not before it.
        typed = AsyncMock(wraps=service.type_text_human_like)
        keyboard = SimpleNamespace(press=AsyncMock(side_effect=press_key), type=AsyncMock(side_effect=type_key))
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
        with _frozen_code():
            result, typed = self.run_autofill("https://login.example.com/2fa", hosts=("login.example.com",))
        self.assertEqual(result["code_length"], 6)
        # One call of the typing code for the whole code, the host asked before each key (B1-fix1 item 4); the keys
        # are the code's, in order (item 29, staged F14).
        typed.assert_awaited_once()
        self.assertEqual("".join(self.typed_keys), CODE)

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
                self.assertEqual(self.typed_keys, [])
                self.pressed.assert_not_awaited()
                # The page left as select-all went in: no Delete into the page it went to (B1-fix1 item 3, Opus A12c).
                if step == "clear by keys":
                    self.assertEqual(self.keys.await_args_list, [call("Control+a")])

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
        with _frozen_code():
            result, typed = self.run_autofill(
                "https://login.example.com/2fa", hosts=("login.example.com",), fill_fails=True
            )
        self.assertEqual(result["code_length"], 6)
        self.assertEqual("".join(self.typed_keys), CODE)
        self.assertEqual(self.keys.await_args_list, [call("Control+a"), call("Delete")])

    def test_a_focused_field_whose_page_leaves_as_its_fill_fails_gets_no_keys(self) -> None:
        # B1-fix1 item 29 (staged F12): the host is asked before select-all on its own. Here the field keeps focus,
        # so only the host's check keeps select-all and Delete off the page the failed fill left the named host for.
        result, typed = self.run_autofill(
            "https://login.example.com/2fa", hosts=("login.example.com",), leaves_during="clear", fill_fails=True
        )
        self.assertIsNone(result)
        self.keys.assert_not_awaited()
        typed.assert_not_awaited()

    def test_a_page_that_leaves_the_named_host_as_its_field_is_asked_for_focus_gets_no_keys(self) -> None:
        # B1-fix2 item 8 (the Opus correctness read, F7): before each key the host was asked, then the field whether
        # focus was in it, a round trip in which the page can leave, and select-all went to the page it left for. The
        # host is asked once focus has answered, with nothing awaited between it and the key.
        result, typed = self.run_autofill(
            "https://login.example.com/2fa",
            hosts=("login.example.com",),
            leaves_during="focus question",
            fill_fails=True,
        )
        self.assertIsNone(result)
        self.keys.assert_not_awaited()
        typed.assert_not_awaited()

    def test_a_clear_by_keys_the_page_does_not_acknowledge_ends_the_autofill_with_no_code(self) -> None:
        # B1-fix2 item 14 (the Opus correctness read, F14; pre-existing): the keys that empty a field whose fill failed
        # waited for the page with no bound, unlike a type's clear (_clear_key). Each is bounded at
        # HUMAN_TYPING_STEP_FLOOR_SECONDS, past which the autofill ends with no code typed.
        with patch("app.browser.services.actions.HUMAN_TYPING_STEP_FLOOR_SECONDS", 0.1):
            began = time.monotonic()
            result, typed = self.run_autofill(
                "https://login.example.com/2fa", hosts=("login.example.com",), fill_fails=True, keys_take=1.0
            )
            took = time.monotonic() - began
        self.assertIsNone(result)
        typed.assert_not_awaited()
        self.assertEqual(self.keys.await_args_list, [call("Control+a")])
        self.assertLess(took, 0.6)

    def test_a_field_that_lost_focus_when_its_fill_failed_gets_no_keys(self) -> None:
        # The keys that empty a field whose fill failed go to whatever has focus: into another field of the page,
        # which the Delete emptied and the code went into. They are pressed only where focus is in the field.
        result, typed = self.run_autofill(
            "https://login.example.com/2fa", hosts=("login.example.com",), fill_fails=True, focused=False
        )
        self.assertIsNone(result)
        self.keys.assert_not_awaited()
        typed.assert_not_awaited()

    def test_a_field_whose_select_all_sends_focus_elsewhere_gets_no_delete(self) -> None:
        # B1-fix1 item 3 (Codex B1-3, Opus F8): focus was asked before select-all only, so a select-all handler that
        # sent focus to another field of the page had that field emptied by the Delete, and the code typed into it.
        # Focus is asked before the Delete as well.
        result, typed = self.run_autofill(
            "https://login.example.com/2fa", hosts=("login.example.com",), fill_fails=True, focused=[True, False]
        )
        self.assertIsNone(result)
        self.assertEqual(self.keys.await_args_list, [call("Control+a")])
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


class SlowKeyboard:
    """A page that acknowledges each key after ``ack`` seconds; what it acknowledged, in order."""

    def __init__(self, ack: float) -> None:
        self.ack = ack
        self.acknowledged: list[str] = []

    async def _key(self, key: str) -> None:
        await asyncio.sleep(self.ack)
        self.acknowledged.append(key)

    async def type(self, char: str) -> None:
        await self._key(char)

    async def insert_text(self, text: str) -> None:
        await self._key(text)

    async def press(self, key: str) -> None:
        await self._key(key)


class TotpTypingBudgetTests(unittest.TestCase):
    def test_the_code_is_typed_within_one_typing_budget(self) -> None:
        # B1-fix1 item 4 (Codex B1-4, Opus F4): b090cb82 typed the code a digit per call of the typing code, each call
        # with a deadline and a scaling of its own, so a slow page on a named host held the session six budgets (with
        # the budget at 0.5 s and the page answering each key in 0.4 s, all six went in and Enter after it). One call
        # types the code now, the host checked before each key: past the budget the type ends as typing_timeout.
        keyboard = SlowKeyboard(ack=0.4)
        settings = SimpleNamespace(human_typing_min_delay_ms=1, human_typing_max_delay_ms=2)
        manager = SimpleNamespace(
            settings=settings, _settle=AsyncMock(), _page_refused=lambda _page: False, _shown_url=lambda page: page.url
        )
        service = BrowserActionService(manager)
        locator = SimpleNamespace(fill=AsyncMock(), press=AsyncMock())
        session = SimpleNamespace(
            page=SimpleNamespace(url="https://login.example.com/2fa", keyboard=keyboard),
            totp_secret=SECRET,
            totp_hosts=("login.example.com",),
        )

        async def find_the_field(_page: object) -> tuple[object, str]:
            return locator, 'input[name*="code" i]'

        service._find_one_time_code_field = find_the_field
        service.focus_verified = AsyncMock()
        with (
            patch("app.browser.services.actions.HUMAN_TYPING_BUDGET_SECONDS", 0.5),
            patch("app.browser.services.actions.HUMAN_TYPING_STEP_FLOOR_SECONDS", 0.1),
            self.assertRaises(BrowserActionError) as ended,
        ):
            asyncio.run(service.maybe_handle_totp(session))

        self.assertEqual(ended.exception.code, "typing_timeout")
        self.assertEqual(len(keyboard.acknowledged), 1, keyboard.acknowledged)
        locator.press.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
