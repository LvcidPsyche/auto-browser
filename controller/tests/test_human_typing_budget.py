"""type_text_human_like keeps its per-character delays for short text and stays inside a total time budget for long text.

The budget bounds the whole typing time, keystrokes included, not only the pauses between them."""

from __future__ import annotations

import asyncio
import contextlib
import time
import unittest
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.action_errors import BrowserActionError
from app.browser.services import actions as actions_module
from app.browser.services.actions import BrowserActionService

MIN_DELAY_MS = 40
MAX_DELAY_MS = 130


@contextlib.contextmanager
def _on_a_stand_in_clock(sleeps: list[float]) -> Iterator[list[float]]:
    """The running loop's clock, which the budget's deadline reads, as a stand-in that moves only by each pause
    (recorded in ``sleeps``) and by what the caller adds to the clock it is handed: the deadline is then reached by
    the text, and not by how fast the host runs it. On the loop's own clock, 5,000 keystrokes of an AsyncMock took
    past the 20 s budget on a slow host, and the rest was rightly inserted."""
    loop = asyncio.get_running_loop()
    clock = [loop.time()]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    with patch.object(actions_module.asyncio, "sleep", sleep), patch.object(loop, "time", lambda: clock[0]):
        yield clock


class HumanTypingBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def _type(self, text: str) -> list[float]:
        settings = SimpleNamespace(human_typing_min_delay_ms=MIN_DELAY_MS, human_typing_max_delay_ms=MAX_DELAY_MS)
        service = BrowserActionService(SimpleNamespace(settings=settings))
        page = SimpleNamespace(keyboard=SimpleNamespace(type=AsyncMock(), insert_text=AsyncMock()))
        sleeps: list[float] = []

        with _on_a_stand_in_clock(sleeps):
            await service.type_text_human_like(page, text)

        self.assertEqual(page.keyboard.type.await_count, len(text))
        page.keyboard.insert_text.assert_not_awaited()
        return sleeps

    async def test_a_long_text_finishes_within_the_budget(self) -> None:
        sleeps = await self._type("x" * 5000)

        self.assertEqual(len(sleeps), 5000)
        self.assertLessEqual(sum(sleeps), actions_module.HUMAN_TYPING_BUDGET_SECONDS + 0.001)

    async def test_a_short_text_keeps_its_human_like_delays(self) -> None:
        sleeps = await self._type("hello world")

        self.assertEqual(len(sleeps), 11)
        self.assertTrue(all(delay >= MIN_DELAY_MS / 1000 for delay in sleeps), sleeps)
        self.assertLess(sum(sleeps), actions_module.HUMAN_TYPING_BUDGET_SECONDS)

    async def test_slow_keystrokes_cannot_outlast_the_budget(self) -> None:
        # Each keystroke is a round trip to a browser node, outside the pauses. Once the budget is spent the rest of
        # the text goes in one insert_text call instead of one round trip per character.
        typed: list[str] = []

        async def slow_type(char: str) -> None:
            await asyncio.sleep(0.01)
            typed.append(char)

        settings = SimpleNamespace(human_typing_min_delay_ms=MIN_DELAY_MS, human_typing_max_delay_ms=MAX_DELAY_MS)
        service = BrowserActionService(SimpleNamespace(settings=settings))
        keyboard = SimpleNamespace(type=slow_type, insert_text=AsyncMock())
        text = "".join(chr(ord("a") + index % 26) for index in range(500))

        started = time.monotonic()
        with patch.object(actions_module, "HUMAN_TYPING_BUDGET_SECONDS", 0.2):
            await service.type_text_human_like(SimpleNamespace(keyboard=keyboard), text)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0)
        keyboard.insert_text.assert_awaited_once()
        rest = keyboard.insert_text.await_args.args[0]
        self.assertTrue(rest)
        self.assertEqual("".join(typed) + rest, text)

    async def _type_past_the_deadline(self, text: str) -> list[tuple[str, str]]:
        """Type `text` with the budget already spent, so all of it goes through the insert path; the calls made."""
        calls: list[tuple[str, str]] = []

        async def record(kind: str, value: str) -> None:
            calls.append((kind, value))

        async def insert(value: str) -> None:
            await record("insert", value)

        async def press(value: str) -> None:
            await record("press", value)

        keyboard = SimpleNamespace(type=AsyncMock(), insert_text=insert, press=press)
        settings = SimpleNamespace(human_typing_min_delay_ms=MIN_DELAY_MS, human_typing_max_delay_ms=MAX_DELAY_MS)
        service = BrowserActionService(SimpleNamespace(settings=settings))

        with patch.object(actions_module, "HUMAN_TYPING_BUDGET_SECONDS", 0.0):
            await service.type_text_human_like(SimpleNamespace(keyboard=keyboard), text)

        keyboard.type.assert_not_awaited()
        return calls

    async def test_past_the_deadline_a_newline_is_still_pressed_as_enter_and_a_tab_goes_in_as_text(self) -> None:
        # insert_text of a newline into an <input> drops it, so a trailing newline that should submit would not. A
        # tab was pressed as Tab too, which moves focus on: typed in time it is a character of the text
        # (keyboard.type has no key for it), so the same text went to other fields by how long it took.
        calls = await self._type_past_the_deadline("ab\ncd\te\r")

        self.assertEqual(calls, [("insert", "ab"), ("press", "Enter"), ("insert", "cd\te"), ("press", "Enter")])

    async def test_a_deadline_passed_partway_inserts_the_rest_with_its_newline_pressed_and_its_tab_as_text(
        self,
    ) -> None:
        # Each keystroke a round trip of 10 ms on the stand-in clock: 4,000 of them outlast the 20 s budget, so the
        # deadline passes partway, as on a slow host, and the rest, a newline and a tab among it, is inserted.
        calls: list[tuple[str, str]] = []
        settings = SimpleNamespace(human_typing_min_delay_ms=MIN_DELAY_MS, human_typing_max_delay_ms=MAX_DELAY_MS)
        service = BrowserActionService(SimpleNamespace(settings=settings))
        text = "x" * 4000 + "ab\ncd\te"

        with _on_a_stand_in_clock([]) as clock:

            async def type_(char: str) -> None:
                calls.append(("type", char))
                clock[0] += 0.01

            async def insert(value: str) -> None:
                calls.append(("insert", value))

            async def press(value: str) -> None:
                calls.append(("press", value))

            keyboard = SimpleNamespace(type=type_, insert_text=insert, press=press)
            await service.type_text_human_like(SimpleNamespace(keyboard=keyboard), text)

        typed = len([call for call in calls if call[0] == "type"])
        # 20 s of keystrokes at 10 ms each, less the pauses between them.
        self.assertTrue(0 < typed < 2000, typed)
        self.assertEqual(calls[:typed], [("type", "x")] * typed)
        self.assertEqual(
            calls[typed:],
            [("insert", "x" * (4000 - typed) + "ab"), ("press", "Enter"), ("insert", "cd\te")],
        )

    async def test_past_the_deadline_plain_text_is_one_insert_and_consecutive_control_keys_are_each_pressed(
        self,
    ) -> None:
        self.assertEqual(await self._type_past_the_deadline("plain text"), [("insert", "plain text")])
        self.assertEqual(
            await self._type_past_the_deadline("\n\nx"), [("press", "Enter"), ("press", "Enter"), ("insert", "x")]
        )

    async def test_a_keystroke_the_page_never_answers_is_an_error_not_to_retry_not_a_hang(self) -> None:
        async def never(*_args) -> None:
            await asyncio.Event().wait()

        settings = SimpleNamespace(human_typing_min_delay_ms=MIN_DELAY_MS, human_typing_max_delay_ms=MAX_DELAY_MS)
        service = BrowserActionService(SimpleNamespace(settings=settings))
        # (keyboard, text, budget): the insert and a control key only run past the deadline, so those cases have no
        # budget left; with one, a fast clock types every key in time and never reaches the insert.
        cases = {
            "a keystroke": (SimpleNamespace(type=never, insert_text=AsyncMock(), press=AsyncMock()), "hello", 0.1),
            "the final insert": (SimpleNamespace(type=AsyncMock(), insert_text=never, press=AsyncMock()), "hello", 0.0),
            "a control key": (SimpleNamespace(type=AsyncMock(), insert_text=AsyncMock(), press=never), "\n", 0.0),
        }
        for name, (keyboard, text, budget) in cases.items():
            with self.subTest(hang=name):
                started = time.monotonic()
                with (
                    patch.object(actions_module, "HUMAN_TYPING_BUDGET_SECONDS", budget),
                    patch.object(actions_module, "HUMAN_TYPING_STEP_FLOOR_SECONDS", 0.1),
                ):
                    with self.assertRaises(BrowserActionError) as raised:
                        await asyncio.wait_for(
                            service.type_text_human_like(SimpleNamespace(keyboard=keyboard), text), timeout=5
                        )
                self.assertLess(time.monotonic() - started, 2.0)
                self.assertEqual(raised.exception.code, "typing_timeout")
                # D3a-7: part of the text may be in the field, so typing it again could enter it twice.
                self.assertFalse(raised.exception.retryable)

    async def test_past_the_deadline_every_step_shares_one_cutoff(self) -> None:
        # Each press answers in time on its own, but a hundred of them must not run a hundred floors past the budget.
        presses: list[str] = []

        async def slow_press(value: str) -> None:
            presses.append(value)
            await asyncio.sleep(0.05)

        keyboard = SimpleNamespace(type=AsyncMock(), insert_text=AsyncMock(), press=slow_press)
        settings = SimpleNamespace(human_typing_min_delay_ms=MIN_DELAY_MS, human_typing_max_delay_ms=MAX_DELAY_MS)
        service = BrowserActionService(SimpleNamespace(settings=settings))

        started = time.monotonic()
        with (
            patch.object(actions_module, "HUMAN_TYPING_BUDGET_SECONDS", 0.0),
            patch.object(actions_module, "HUMAN_TYPING_STEP_FLOOR_SECONDS", 0.2),
        ):
            with self.assertRaises(BrowserActionError) as raised:
                await asyncio.wait_for(
                    service.type_text_human_like(SimpleNamespace(keyboard=keyboard), "\n" * 100), timeout=10
                )

        self.assertLess(time.monotonic() - started, 1.0)  # the cutoff is 0.2 s past the deadline
        self.assertLess(len(presses), 20)
        self.assertEqual(raised.exception.code, "typing_timeout")
        self.assertFalse(raised.exception.retryable)

    async def test_the_budget_is_twenty_seconds(self) -> None:
        self.assertEqual(actions_module.HUMAN_TYPING_BUDGET_SECONDS, 20.0)


if __name__ == "__main__":
    unittest.main()
