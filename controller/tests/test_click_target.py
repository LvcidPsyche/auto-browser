"""STORE-42: a fast-profile click labelled read or write is classified by what it lands on, not the model's word.

classified_risk reads DESCRIBE_TARGET_SCRIPT's answer; these pin its table one case at a time, and where
read_click_target sends each way of naming a target. The scripts themselves, and the fast path that uses them, run
against real Chromium in test_click_target_chromium.py.
"""

from __future__ import annotations

import asyncio
import logging
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.action_errors import PAGE_NOT_RESPONDING, BrowserActionError
from app.browser import click_target
from app.browser.click_target import (
    CLICK_LABEL_PHRASES,
    CLICK_TARGET_SCRIPT,
    DESCRIBE_TARGET_SCRIPT,
    MAX_LABEL_CHARS,
    READ_TIMEOUT_MS,
    classified_risk,
    read_click_target,
)
from app.models import BrowserActionDecision


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
            ("post", "Publish"),
        ):
            for label_as in ("read", "write"):
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

    def test_role_button_and_input_buttons_count_and_a_link_does_not(self) -> None:
        self.assertEqual(classified_risk(button("Remove", tag="div", type="", role="button"), "read"), "destructive")
        self.assertEqual(classified_risk(button("Send", tag="input", type="image"), "write"), "post")
        self.assertIsNone(classified_risk(button("Delete your account", tag="a", type=""), "read"))
        self.assertIsNone(classified_risk(button("Delete", tag="input", type="text"), "read"))

    def test_a_read_labelled_form_submit_is_recorded_as_write(self) -> None:
        submit = button("Search", type="submit", submits=True)
        self.assertEqual(classified_risk(submit, "read"), "write")
        self.assertIsNone(classified_risk(submit, "write"))

    def test_a_write_labelled_click_on_a_normal_button_is_left_alone(self) -> None:
        self.assertIsNone(classified_risk(button("Save draft"), "write"))
        self.assertIsNone(classified_risk(button("Show more"), "read"))

    def test_other_labels_and_unreadable_targets_change_nothing(self) -> None:
        for label in ("payment", "post", "account_change", "destructive", "upload", None):
            with self.subTest(label=label):
                self.assertIsNone(classified_risk(button("Delete", type="submit", submits=True), label))
        for target in (None, "button", [], {"tag": 1, "labels": "Delete"}, {}):
            with self.subTest(target=target):
                self.assertIsNone(classified_risk(target, "read"))


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
        return await read_click_target(element_ids, page or _Page(), decision)

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
        self.assertEqual(page.located, [])

    async def test_a_selector_is_read_on_the_element_the_click_would_find(self) -> None:
        element_ids = AsyncMock()
        limit = MAX_LABEL_CHARS * 4

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

    async def test_a_page_that_does_not_answer_classifies_nothing_and_is_logged_at_debug(self) -> None:
        element_ids = AsyncMock()
        element_ids.evaluate.side_effect = BrowserActionError("no answer", code=PAGE_NOT_RESPONDING)

        with self.assertLogs("app.browser.click_target", level=logging.DEBUG) as logged:
            self.assertIsNone(await self._read(element_ids, x=10.0, y=20.0))

        self.assertEqual([record.levelno for record in logged.records], [logging.DEBUG])

    async def test_a_malformed_element_id_classifies_nothing(self) -> None:
        element_ids = AsyncMock()
        with self.assertLogs("app.browser.click_target", level=logging.DEBUG):
            self.assertIsNone(await self._read(element_ids, element_id="not-an-id"))
        element_ids.evaluate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
