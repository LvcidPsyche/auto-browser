"""STORE-42: a fast-profile click labelled read or write is classified by what it lands on, not the model's word.

classified_risk reads DESCRIBE_TARGET_SCRIPT's answer; these pin its table one case at a time, and where
read_click_target sends each way of naming a target. The scripts themselves, and the fast path that uses them, run
against real Chromium in test_click_target_chromium.py.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import unittest
from types import SimpleNamespace
from typing import Any, get_args
from unittest.mock import AsyncMock, patch

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.action_errors import PAGE_NOT_RESPONDING, BrowserActionError
from app.approval_display import APPROVAL_SUMMARY_CUT
from app.browser import click_target
from app.browser.click_target import (
    CHECKED_LABELS,
    CLICK_LABEL_PHRASES,
    CLICK_TARGET_SCRIPT,
    DESCRIBE_TARGET_SCRIPT,
    HARMLESS_AFTER_SEND,
    HARMLESS_LABEL_PHRASES,
    MAX_LABEL_CHARS,
    READ_TIMEOUT_MS,
    SEND_DESTINATION_WORDS,
    SEND_FILLER_WORDS,
    SEND_LINK_WORDS,
    SEND_TAIL_WORDS,
    classified_reason,
    classified_risk,
    read_click_target,
)
from app.browser.services.actions import BrowserActionService
from app.models import BrowserActionDecision, RiskCategory


def button(*labels: str, tag: str = "button", type: str = "button", role: str = "", submits: bool = False) -> dict:
    return {"tag": tag, "type": type, "role": role, "submits": submits, "labels": list(labels)}


class ClassifiedRiskTests(unittest.TestCase):
    def test_each_kind_of_the_table_is_matched_on_a_button(self) -> None:
        for kind, label in (
            ("payment", "Place order"),
            ("payment", "Pay $20.00"),
            ("destructive", "Delete account"),
            ("destructive", "Cancel subscription"),
            ("account_change", "Change password"),
            ("account_change", "Change e-mail"),
            ("account_change", "Update e-mail address"),
            ("post", "Publish"),
        ):
            for label_as in ("read", "write", "upload"):
                with self.subTest(label=label, label_as=label_as):
                    self.assertEqual(classified_risk(button("", label), label_as), kind)

    def test_every_phrase_of_the_table_matches_itself(self) -> None:
        for kind, phrases in CLICK_LABEL_PHRASES.items():
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    self.assertEqual(classified_risk(button(phrase.upper()), "read"), kind)

    def test_the_common_harmless_buttons_are_not_matched(self) -> None:
        for label in ("Close", "Cancel", "Dismiss", "OK", "Continue", "Next", "Accept", "Cancel anytime"):
            with self.subTest(label=label):
                self.assertIsNone(classified_risk(button(label), "write"))

    def test_whole_words_at_the_start_only(self) -> None:
        for label in ("Deleted items", "Payment details", "Postal code", "Undo delete", "Sender info"):
            with self.subTest(label=label):
                self.assertIsNone(classified_risk(button(label), "read"))

    def test_case_spacing_and_a_leading_icon_do_not_hide_a_label(self) -> None:
        self.assertEqual(classified_risk(button("\N{WASTEBASKET}  DELETE\n account"), "read"), "destructive")

    def test_a_long_label_is_not_matched(self) -> None:
        long_label = "Delete " + "x" * 40
        self.assertIsNone(classified_risk(button(long_label), "read"))
        self.assertEqual(classified_risk(button("Delete " + "x" * 33), "read"), "destructive")

    def test_any_of_its_labels_matches(self) -> None:
        # The accessible label may say one thing and the text another: either is the button's.
        self.assertEqual(classified_risk(button("Help", "Delete"), "read"), "destructive")
        self.assertEqual(
            classified_risk(button("", "", "Place order", "", tag="input", type="submit"), "read"), "payment"
        )

    def test_a_fuller_harmless_name_of_the_button_clears_its_short_one(self) -> None:
        # A resend button drawn as "Send" and named "Send code" for a screen reader, a filter chip drawn as
        # "Remove": each label was read apart, and the short one asked.
        for labels in (("Send code", "Send"), ("Send", "Send code"), ("Remove filter: Price", "Remove")):
            with self.subTest(labels=labels):
                self.assertIsNone(classified_risk(button(*labels), "write"))
        # Only a name that goes on from the very words that ask, harmlessly.
        self.assertEqual(classified_risk(button("Remove filter", "Delete account"), "write"), "destructive")
        both = button("Remove", "Remove from library and delete files")
        self.assertEqual(classified_risk(both, "write"), "destructive")
        self.assertEqual(classified_risk(button("Send payment link", "Send"), "write"), "post")
        self.assertEqual(classified_risk(button("Send", "Send link to everyone"), "write"), "post")
        self.assertEqual(classified_risk(button("Sending code", "Send"), "write"), "post")

    def test_role_button_and_input_buttons_count_and_a_link_does_not(self) -> None:
        self.assertEqual(classified_risk(button("Remove", tag="div", type="", role="button"), "read"), "destructive")
        self.assertEqual(classified_risk(button("Send", tag="input", type="image"), "write"), "post")
        self.assertIsNone(classified_risk(button("Delete your account", tag="a", type=""), "read"))
        self.assertIsNone(classified_risk(button("Delete", tag="input", type="text"), "read"))

    def test_a_menu_item_counts_as_a_button_and_a_choice_in_a_menu_or_a_list_does_not(self) -> None:
        # The "Delete" of a row's menu is a menu item in the common component libraries, on a li or a div.
        item = button("Delete account", tag="li", type="", role="menuitem")
        self.assertEqual(classified_risk(item, "read"), "destructive")
        self.assertEqual(classified_risk(button("Send", tag="div", type="", role="menuitem"), "write"), "post")
        self.assertIsNone(classified_risk(button("Rename", tag="li", type="", role="menuitem"), "read"))
        # What chooses or switches is not a command: a billing period, a list's option, a tab.
        # The page's walk to the button knows the same roles: it is given them as a list of its own.
        listed = ", ".join(f"'{role}'" for role in sorted(click_target._BUTTON_ROLES))
        self.assertIn(f"const ROLES = [{listed}];", click_target.DESCRIBE_TARGET_SCRIPT)
        for role in ("menuitemradio", "menuitemcheckbox", "option", "tab", "switch"):
            with self.subTest(role=role):
                self.assertIsNone(classified_risk(button("Pay monthly", tag="li", type="", role=role), "read"))

    def test_a_read_labelled_form_submit_is_recorded_as_write(self) -> None:
        submit = button("Search", type="submit", submits=True)
        self.assertEqual(classified_risk(submit, "read"), "write")
        self.assertIsNone(classified_risk(submit, "write"))

    def test_a_write_labelled_click_on_a_normal_button_is_left_alone(self) -> None:
        self.assertIsNone(classified_risk(button("Save draft"), "write"))
        self.assertIsNone(classified_risk(button("Show more"), "read"))

    def test_other_labels_and_unreadable_targets_change_nothing(self) -> None:
        for label in ("payment", "post", "account_change", "destructive", None):
            with self.subTest(label=label):
                self.assertIsNone(classified_risk(button("Delete", type="submit", submits=True), label))
        for target in (None, "button", [], {"tag": 1, "labels": "Delete"}, {}):
            with self.subTest(target=target):
                self.assertIsNone(classified_risk(target, "read"))

    def test_every_label_a_click_can_carry_asks_by_itself_or_has_its_target_read(self) -> None:
        # "upload" on a click asked for nothing (only the upload action asks for that kind) and was not read: a
        # third label under which "Delete account" ran as the model called it.
        settings = SimpleNamespace(require_approval_for_uploads=True)
        service = BrowserActionService(SimpleNamespace(settings=settings))
        for label in get_args(RiskCategory):
            click = BrowserActionDecision(action="click", reason="r", risk_category=label, selector="#delete")
            with self.subTest(label=label):
                self.assertTrue(service.approval_kind_for_decision(click) is not None or label in CHECKED_LABELS)


class LabelRulesTests(unittest.TestCase):
    """The rules a label is read by, one table of labels that must ask and one of labels that must not."""

    MUST_ASK = (
        # Each line on its own: a name on one line and a sentence under it.
        ("destructive", "Delete account\nThis permanently removes your profile and all of its data"),
        ("payment", "Premium plan, billed monthly until you cancel it\nPay $20.00\nCancel anytime"),
        ("post", "Share\n\n\nPublish"),
        # A phrase a line break splits is still read whole.
        ("payment", "Place\norder"),
        # Leading tokens that are not words: a number, an icon font's ligature name.
        ("destructive", "1 Delete"),
        ("destructive", "2 \N{MIDDLE DOT} Remove"),
        ("payment", "shopping_cart Buy now"),
        ("payment", "credit_card Pay now"),
        ("destructive", "delete_forever"),
        ("destructive", "remove_circle_outline"),
        # Invisible characters.
        ("destructive", "De​lete"),
        ("payment", "Pay­ now"),
        ("destructive", "﻿Remove"),
        ("post", "Pu‌blish"),
        ("post", "Se‍nd"),
        ("payment", "⁠Purchase"),
        ("destructive", "De\N{LEFT-TO-RIGHT MARK}lete account"),
        ("destructive", "Rem\N{FIRST STRONG ISOLATE}ove"),
        ("post", "Pub\N{COMBINING GRAPHEME JOINER}lish"),
        # "Send" is let go only for a way to sign in: nothing else may stand before the code or the link, and
        # only where it goes after it.
        ("post", "Send payment link"),
        ("post", "Send gift code"),
        ("post", "Send invite link"),
        ("post", "Send source code"),
        ("post", "Send message with link"),
        ("post", "Send a code review request"),
        ("post", "Send code and delete the account"),
        ("post", "Send verification documents"),
        ("post", "Send verification"),
        ("post", "Send email"),
        # An icon font's name of one word before the label, as the icon's element puts it on the label's line.
        ("payment", "payment Pay now"),
        ("payment", "lock Pay securely"),
        ("destructive", "warning Delete account"),
        ("destructive", "cancel Cancel subscription"),
        ("payment", "check Confirm purchase"),
        ("destructive", "close Close account"),
        ("destructive", "block Deactivate"),
        ("destructive", "arrow back Delete account"),
        # Lower case first is still the label where no capitalised word follows it, and where one does and
        # says nothing.
        ("destructive", "delete account"),
        ("destructive", "delete Account"),
        # What Send sends is not a way to sign in, or may not be.
        ("post", "Send SMS"),
        ("post", "Send text message"),
        ("post", "Send again"),
        ("post", "Send password"),
        ("post", "Send instructions"),
        # A pin is also what a map or a board sends to somebody.
        ("post", "Send PIN"),
        # And a link is what a share dialog's button sends, to whomever its form names: a way to sign in only
        # where a word before it says so.
        ("post", "Send link"),
        ("post", "Send a link"),
        ("post", "Send new link"),
        ("post", "Send link by email"),
        ("post", "Send link to John"),
        ("post", "Send link to everyone"),
        ("post", "Send email link"),
        # After "to", "via" and the like a label may say only where the code goes or what it is for: the user's
        # own address or the sign-in it serves. Sent to somebody else it is a message, however it starts.
        ("post", "Send code to all users"),
        ("post", "Send code to everyone"),
        ("post", "Send code to John"),
        ("post", "Send code to the whole team"),
        ("post", "Send code to @channel"),
        ("post", "Send code via the group chat"),
        ("post", "Send verification email to all users"),
        ("post", "Send login link to all members"),
        ("post", "Send reset link to customer"),
        ("post", "Send code to verify payment"),
        ("post", "Send code to confirm account deletion"),
        # A masked number is digits and marks: a name with a digit or an asterisk in it is somebody's name, and
        # a word behind @ or # names a person or a channel, whatever the word is.
        ("post", "Send code to team2"),
        ("post", "Send code to user#123"),
        ("post", "Send code to #general2"),
        ("post", "Send code to *everyone*"),
        ("post", "Send code to *all* *users*"),
        ("post", "Send code to 5 *people*"),
        ("post", "Send code to #work"),
        ("post", "Send code to @backup"),
        ("post", "Send code to (@backup)"),
        # The everyday words name no one: beside a word that does, the label still asks.
        ("post", "Send code to all trusted users"),
        ("post", "Send code to the same people \u2192"),
        # Past 40 characters a label is read line by line, and "Send code" alone is a sign-in button's name:
        # a next line that opens with "to", "via" and the like goes on with where the code is sent.
        ("post", "Send code\nto all users in the organization"),
        ("post", "Send verification code\nvia the group chat for everyone"),
        ("post", "Send me a login link\nTo: the whole engineering team"),
        # An icon's name that is itself a phrase of the table asks, whatever follows it: with a verb in lower
        # case, "delete Purchase history" is a label too.
        ("destructive", "delete Purchase history"),
        ("destructive", "delete Remove filter"),
        ("post", "send Send code"),
        # A harmless line does not excuse a dangerous one beside it.
        ("destructive", "Remove filter\nDelete account"),
        ("destructive", "Delete account\nRemove filter"),
        # Nor a harmless start what its own line goes on to.
        ("destructive", "Remove from library and delete files"),
        ("destructive", "Remove filter and delete the account"),
        ("destructive", "remove filter delete account"),
        ("destructive", "Remove from list & Erase data"),
        ("payment", "Purchase history and delete the account"),
        ("payment", "Pay as you go: Buy credits"),
        # The phrases added.
        ("payment", "Place your order"),
        ("payment", "Buy it now"),
        ("payment", "Buy tickets"),
        ("payment", "Confirm order"),
        ("payment", "Confirm and pay"),
        ("payment", "Complete order"),
        ("payment", "Submit payment"),
        ("payment", "Submit order"),
        ("payment", "Order now"),
        ("payment", "Donate"),
        ("payment", "Transfer"),
        ("destructive", "Yes, delete"),
        ("destructive", "Yes delete it"),
        ("destructive", "Yes, remove"),
        ("destructive", "Confirm delete"),
        ("destructive", "Confirm deletion"),
        ("destructive", "Erase"),
        ("destructive", "Empty trash"),
        ("destructive", "Deactivate"),
        ("destructive", "Deactivate account"),
        # What a harmless continuation must not swallow.
        ("post", "Send"),
        ("post", "Send message"),
        ("post", "Send codes of conduct"),
        ("destructive", "Remove"),
        ("destructive", "Remove member"),
        ("destructive", "Remove filtered items"),
        ("destructive", "Delete draft"),
        ("payment", "Pay"),
        ("payment", "Pay - $20.00"),
        ("payment", "Purchase"),
        ("post", "Post"),
    )
    MUST_NOT_ASK = (
        # A hyphen is part of a word.
        "Pay-as-you-go",
        "Post-processing",
        "Send-off party",
        "Delete‐protected",
        # Harmless continuations of a phrase.
        "Send code",
        "Send me a login link",
        "Send reset link",
        "Send OTP",
        "Send one-time passcode",
        "Send verification email",
        "Send a new code",
        "Send password reset link",
        "Send SMS code",
        "Send magic link",
        "Send code to my phone",
        "Send code again",
        # Where the code goes, as a page shows the user's own address, and what it is for.
        "Send code to my email",
        "Send code to my phone.",
        "Send code to j***@example.com",
        "Send code to me at j***@example.com",
        "Send code to +1 (***) ***-1234",
        "Send code to \u2022\u2022\u2022\u20221234",
        "Send code to phone ending in 1234",
        "Send code via SMS",
        "Send code by text message",
        "Send me a code via WhatsApp",
        "Send code to authenticator app",
        "Send code to a different number",
        "Send code to another device",
        "Send code again in 30s",
        "Send code to ***-***-1234",
        "Send code to xxx-xxx-1234",
        "Send code to (555) 555-0199",
        "Send code to number ending 1234.",
        "Send code to phone #1234",
        "Send code to *my* phone",
        # What a sign-in page says every day, and a mark after it.
        "Send code to my phone \u2192",
        "Send code via SMS \u2713",
        "Send code to my phone \u2013",
        "Send code to your trusted device",
        "Send code to the phone on file",
        "Send code to the number we have on file",
        "Send code to the saved number",
        "Send code to the same number",
        "Send code to this browser",
        "Send code again in 30 seconds",
        "Send code to log into your account",
        "Send code to access your account",
        "Send code to proceed",
        "Send code to verify it's you",
        "Send code to verify it\u2019s you",
        "Send code to you",
        "Send code to my linked email",
        "Send code to the address above",
        "Send code by push notification",
        # The same over two lines, and a name over a sentence about it.
        "Send code\nto the phone number ending in 1234 to continue",
        "Send code\nWe will text a code to the number we have on file",
        "Send code\nYou will get it within a minute or two",
        "Send magic link to my inbox",
        "Send code to reset your password",
        "Send code to verify my email",
        "Send code to sign in",
        "Send code to continue",
        "Send me a link",
        "Send us a new link",
        "Send verification link",
        "Send login link to my email",
        "Send magic link by email",
        # Written without the hyphen.
        "Send me a sign in link",
        "Send log in link",
        "Send one time code",
        "Send one time password",
        "Send me a one time PIN",
        "Remove filter",
        "Remove filters",
        "Remove all filters",
        "Remove from cart",
        # What a harmless label goes on with, where that is no phrase of the table.
        "Remove filter: Brand",
        "Remove from cart (2 items)",
        "Remove from list of non-delete items",
        "Purchase history for 2024",
        "Purchase order #1042",
        "Pay as you go plan",
        # What Send sends to sign in stays one whatever it is for.
        "Send code to reset password",
        "Remove from basket",
        "Remove from compare",
        "Remove from wishlist",
        "Remove from favorites",
        "Remove from favourites",
        "Remove from playlist",
        "Remove from queue",
        "Remove from watchlist",
        "Remove from library",
        "Remove from list",
        "Remove from bag",
        "Send one-time password",
        "Send me a one-time PIN",
        "Send password reset email",
        "Send login email",
        "Send reset instructions",
        "Send password reset instructions",
        # An icon's name before a harmless label, and a file's name with an underscore in it.
        "search Remove filters",
        "history Purchase history",
        "delete_me.txt",
        "post_2024.md",
        "pay_stub.pdf",
        # A harmless continuation is one however the page breaks its lines.
        "Send\ncode",
        "Remove\nfilter",
        "Purchase\nhistory",
        "Remove\nfilter for price, brand, colour and everything else on offer",
        "Purchase history",
        "Purchase order",
        "Purchase orders",
        "Pay as you go",
        "Post code",
        # Left out on purpose.
        "Close",
        "Cancel",
        "Dismiss",
        "OK",
        "Continue",
        "Next",
        "Accept",
        "Cancel anytime",
        "Checkout",
        "Proceed to checkout",
        "Subscribe",
        "Unsubscribe",
        "Submit",
        "Confirm",
        "Save changes",
        "Clear",
        # Not a label at all.
        "1",
        "shopping_cart",
        "arrow_back more_vert",
        "​",
        # Past the first three lines, or a line that is a sentence.
        "Settings\nProfile\nSecurity\nDelete account",
        "Account\nDelete everything you have ever stored with us, for good",
    )

    def test_labels_that_must_ask(self) -> None:
        for kind, label in self.MUST_ASK:
            with self.subTest(label=label):
                self.assertEqual(classified_risk(button(label), "read"), kind)

    def test_labels_that_must_not_ask(self) -> None:
        for label in self.MUST_NOT_ASK:
            with self.subTest(label=label):
                self.assertIsNone(classified_risk(button(label), "write"))

    def test_no_phrase_is_covered_by_a_shorter_one_of_its_kind(self) -> None:
        # "buy now" beside "buy" matches nothing "buy" does not: the table stays what is needed.
        for kind, phrases in CLICK_LABEL_PHRASES.items():
            for phrase in phrases:
                covering = [other for other in phrases if other != phrase and phrase.startswith(other + " ")]
                with self.subTest(kind=kind, phrase=phrase):
                    self.assertEqual(covering, [])

    def test_every_harmless_continuation_starts_with_a_phrase_of_the_table(self) -> None:
        # One that starts with none is dead weight: the table would not have matched it.
        phrases = [phrase for kind in CLICK_LABEL_PHRASES.values() for phrase in kind]
        for harmless in (*HARMLESS_LABEL_PHRASES, "send"):
            with self.subTest(harmless=harmless):
                self.assertTrue(any(harmless == phrase or harmless.startswith(phrase + " ") for phrase in phrases))
        self.assertTrue(HARMLESS_AFTER_SEND)

    def test_what_send_may_be_followed_by_is_three_lists_that_do_not_overlap_in_use(self) -> None:
        # "Send email" must ask: a word that may stand before the code or the link is not one that ends the
        # exception by itself.
        self.assertTrue(SEND_FILLER_WORDS)
        self.assertTrue(SEND_TAIL_WORDS)
        # What may follow a tail word names no kind of the table by itself, and no everybody.
        self.assertTrue(SEND_DESTINATION_WORDS)
        phrases = {phrase for kind in CLICK_LABEL_PHRASES.values() for phrase in kind}
        self.assertEqual(set(SEND_DESTINATION_WORDS) & (phrases | {"all", "everyone", "users", "team"}), set())
        self.assertEqual(set(SEND_FILLER_WORDS) & set(HARMLESS_AFTER_SEND), set())
        self.assertEqual(set(SEND_TAIL_WORDS) & (set(SEND_FILLER_WORDS) | set(HARMLESS_AFTER_SEND)), set())
        # A link is no way to sign in by itself, and every word that makes it one may stand before it.
        self.assertNotIn("link", HARMLESS_AFTER_SEND)
        self.assertTrue(SEND_LINK_WORDS)
        self.assertLessEqual(set(SEND_LINK_WORDS), set(SEND_FILLER_WORDS))

    def test_the_page_hands_over_enough_of_a_label_for_three_lines(self) -> None:
        self.assertGreaterEqual(click_target._LABEL_CUT, 3 * (MAX_LABEL_CHARS + 1))


class ClassifiedReasonTests(unittest.TestCase):
    def test_it_says_who_chose_the_kind_then_gives_the_agents_reason_as_the_agents(self) -> None:
        self.assertEqual(
            classified_reason("payment", "write", "opening the help panel"),
            'The controller reads this click\'s target as a button of kind "payment"; the agent labelled the click '
            '"write". The agent\'s reason: opening the help panel',
        )

    def test_it_names_a_type_as_a_type(self) -> None:
        self.assertEqual(
            classified_reason("destructive", "write", "filling in the form", "type"),
            'The controller reads this type action\'s target as a button of kind "destructive"; the agent labelled '
            'the type action "write". The agent\'s reason: filling in the form',
        )

    def test_asked_at_the_press_it_names_what_the_action_would_press_not_its_target(self) -> None:
        # The action named a div, and what was read is the button under the pointer: "this click's target reads
        # as a button" was said of the div, beside a summary that names the div.
        self.assertEqual(
            classified_reason("destructive", "read", "opening the row", at_the_press=True),
            'The controller reads what this click would press as a button of kind "destructive"; the agent labelled '
            'the click "read". The agent\'s reason: opening the row',
        )
        self.assertEqual(
            classified_reason("payment", "write", "filling in the form", "type", at_the_press=True),
            'The controller reads what this type action would press as a button of kind "payment"; the agent '
            'labelled the type action "write". The agent\'s reason: filling in the form',
        )

    def test_the_controllers_sentence_fits_what_a_short_account_of_an_approval_shows(self) -> None:
        # The dashboard's confirm dialog and the summaries cut a field at APPROVAL_SUMMARY_CUT characters: a
        # long reason of the agent's must not push the controller's sentence out of them.
        for kind in CLICK_LABEL_PHRASES:
            for label, action in (("read", "click"), ("write", "click"), ("upload", "type")):
                for at_the_press in (False, True):
                    with self.subTest(kind=kind, label=label, action=action, at_the_press=at_the_press):
                        reason = classified_reason(kind, label, "x" * 1000, action, at_the_press=at_the_press)
                        self.assertLess(reason.index("x" * 1000), APPROVAL_SUMMARY_CUT)
                        self.assertTrue(reason.endswith("The agent's reason: " + "x" * 1000))


class _Locator:
    """What page.locator(selector).first is asked, and what it answers (a value, an exception, or never)."""

    def __init__(self, page: "_Page") -> None:
        self._page = page

    @property
    def first(self) -> "_Locator":
        return self

    async def evaluate(self, expression: str, arg: Any = None, *, timeout: float | None = None) -> Any:
        self._page.evaluated.append((expression, arg, timeout))
        if self._page.answer is NEVER:
            try:
                await asyncio.Event().wait()
            finally:
                self._page.left += 1
        if isinstance(self._page.answer, BaseException):
            raise self._page.answer
        return self._page.answer


NEVER = object()


class _Page:
    def __init__(self, answer: Any = None) -> None:
        self.answer = answer
        self.located: list[str] = []
        self.evaluated: list[tuple[str, Any, float | None]] = []
        self.left = 0

    def locator(self, selector: str) -> _Locator:
        self.located.append(selector)
        return _Locator(self)


class ReadClickTargetTests(unittest.IsolatedAsyncioTestCase):
    async def _read(self, element_ids: Any, page: _Page | None = None, **fields: Any) -> Any:
        decision = BrowserActionDecision(action="click", reason="r", risk_category="read", **fields)
        return await read_click_target(element_ids, page or _Page(), decision, session_id="session-7")

    async def test_an_element_id_or_a_point_is_read_in_the_world_and_bounded(self) -> None:
        element_ids = AsyncMock()
        element_ids.evaluate.return_value = {"tag": "button"}
        element_id = "op-" + "ab" * 12
        page = _Page()

        for fields, expected in (
            ({"element_id": element_id}, {"elementId": element_id}),
            ({"selector": f'[data-operator-id="{element_id}"]'}, {"elementId": element_id}),
            # As the click does: an element id wins over a selector, and a selector over a point.
            ({"element_id": element_id, "selector": "text=Delete"}, {"elementId": element_id}),
            ({"x": 10.0, "y": 20.0}, {"x": 10.0, "y": 20.0}),
        ):
            with self.subTest(fields=fields):
                self.assertEqual(await self._read(element_ids, page, **fields), {"tag": "button"})
                self.assertEqual(element_ids.evaluate.await_args.args[1], CLICK_TARGET_SCRIPT)
                arg = element_ids.evaluate.await_args.args[2]
                self.assertEqual({key: value for key, value in arg.items() if key != "limit"}, expected)
                kwargs = element_ids.evaluate.await_args.kwargs
                self.assertEqual((kwargs["timeout_ms"], kwargs["setup_timeout_ms"]), (READ_TIMEOUT_MS, READ_TIMEOUT_MS))
                # Past its bound the read gives up itself, not the session the page's other calls are on.
                self.assertIs(kwargs["keep_session"], True)
        self.assertEqual(page.located, [])

    async def test_a_selector_is_read_on_the_element_the_click_would_find(self) -> None:
        element_ids = AsyncMock()
        limit = click_target._LABEL_CUT

        for selector in ("#delete", "text=Delete account", 'role=button[name="Delete"]', "xpath=//button"):
            with self.subTest(selector=selector):
                page = _Page({"tag": "button"})
                self.assertEqual(
                    await self._read(element_ids, page, selector=selector, x=1.0, y=2.0), {"tag": "button"}
                )
                # page.locator(selector).first, as BrowserActionService.click resolves it, and nothing but a read.
                self.assertEqual(page.located, [selector])
                self.assertEqual(page.evaluated, [(DESCRIBE_TARGET_SCRIPT, limit, READ_TIMEOUT_MS)])
        element_ids.evaluate.assert_not_awaited()

    async def test_a_selector_whose_read_never_answers_is_given_up_at_the_bound(self) -> None:
        page = _Page(NEVER)
        loop = asyncio.get_running_loop()
        started = loop.time()

        with patch.object(click_target, "READ_TIMEOUT_MS", 50), self.assertLogs("app.browser.click_target", "DEBUG"):
            self.assertIsNone(await self._read(AsyncMock(), page, selector="text=Delete account"))

        self.assertLess(loop.time() - started, 5)
        # The call is cancelled and left behind: it is not waited for.
        await asyncio.sleep(0)
        self.assertEqual(page.left, 1)

    async def test_a_selector_playwright_refuses_or_cannot_find_classifies_nothing(self) -> None:
        for answer in (PlaywrightError("Unexpected token"), PlaywrightTimeoutError("Timeout 2000ms exceeded.")):
            with self.subTest(answer=answer), self.assertLogs("app.browser.click_target", "DEBUG"):
                self.assertIsNone(await self._read(AsyncMock(), _Page(answer), selector="##"))

    async def test_a_cancelled_click_takes_its_read_with_it(self) -> None:
        page = _Page(NEVER)
        reading = asyncio.ensure_future(self._read(AsyncMock(), page, selector="text=Delete account"))
        await asyncio.sleep(0.01)

        reading.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reading

        await asyncio.sleep(0)
        self.assertEqual(page.left, 1)

    async def test_a_click_cancelled_as_its_read_fails_leaves_nothing_for_asyncio_to_report(self) -> None:
        # The read had ended, with an exception, when the click was cancelled: nothing retrieved it, and asyncio
        # reported "Task exception was never retrieved" at error, the exception's text (the page's) with it.
        page = _Page(PlaywrightError("PAGE_PRIVATE_TEXT"))
        loop = asyncio.get_running_loop()
        reported: list[dict[str, Any]] = []
        handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        self.addCleanup(loop.set_exception_handler, handler)
        reading = asyncio.ensure_future(self._read(AsyncMock(), page, selector="text=Gone"))
        await asyncio.sleep(0)  # the read is asked
        await asyncio.sleep(0)  # and has failed; the click has not been told yet

        reading.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reading

        del reading
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        self.assertEqual([context["message"] for context in reported], [])

    async def test_an_unread_click_is_logged_at_info_by_session_and_kind_of_failure_never_its_text(self) -> None:
        # A page that does not answer, a navigation in flight, a selector nothing matches: each runs as labelled,
        # and a deployment could not tell "read and found harmless" from "not read". An exception's text can hold
        # the page's, so only debug carries it.
        page_text = "Delete account <b>now</b>"
        for fields, failure, kind in (
            ({"x": 10.0, "y": 20.0}, BrowserActionError(page_text, code=PAGE_NOT_RESPONDING), "BrowserActionError"),
            ({"x": 10.0, "y": 20.0}, PlaywrightError(page_text), "Error"),
            ({"selector": "text=Gone"}, PlaywrightTimeoutError(page_text), "TimeoutError"),
        ):
            with self.subTest(failure=failure):
                element_ids = AsyncMock()
                element_ids.evaluate.side_effect = failure

                with self.assertLogs("app.browser.click_target", level=logging.DEBUG) as logged:
                    self.assertIsNone(await self._read(element_ids, _Page(failure), **fields))

                levels = [record.levelno for record in logged.records]
                self.assertEqual(sorted(levels), [logging.DEBUG, logging.INFO])
                info = next(record for record in logged.records if record.levelno == logging.INFO).getMessage()
                self.assertIn("session-7", info)
                self.assertIn(kind, info)
                self.assertIn("click labelled read", info)
                self.assertNotIn("Delete account", info)

    async def test_a_target_that_is_read_logs_nothing_whatever_it_is(self) -> None:
        for answer in ({"tag": "button", "labels": ["Delete"]}, {"tag": "a"}, None):
            with self.subTest(answer=answer):
                element_ids = AsyncMock()
                element_ids.evaluate.return_value = answer
                with self.assertNoLogs("app.browser.click_target", level=logging.DEBUG):
                    self.assertEqual(await self._read(element_ids, x=10.0, y=20.0), answer)

    async def test_a_malformed_element_id_classifies_nothing(self) -> None:
        element_ids = AsyncMock()
        with self.assertLogs("app.browser.click_target", level=logging.DEBUG):
            self.assertIsNone(await self._read(element_ids, element_id="not-an-id"))
        element_ids.evaluate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
