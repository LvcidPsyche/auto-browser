"""type_text_human_like keeps its per-character delays for short text and stays inside a total time budget for long text.

The budget bounds the whole typing time, keystrokes included, not only the pauses between them."""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.browser.services import actions as actions_module
from app.browser.services.actions import BrowserActionService

MIN_DELAY_MS = 40
MAX_DELAY_MS = 130


class HumanTypingBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def _type(self, text: str) -> list[float]:
        settings = SimpleNamespace(human_typing_min_delay_ms=MIN_DELAY_MS, human_typing_max_delay_ms=MAX_DELAY_MS)
        service = BrowserActionService(SimpleNamespace(settings=settings))
        page = SimpleNamespace(keyboard=SimpleNamespace(type=AsyncMock(), insert_text=AsyncMock()))
        sleeps: list[float] = []

        async def record(seconds: float) -> None:
            sleeps.append(seconds)

        with patch.object(actions_module.asyncio, "sleep", record):
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

    async def test_the_budget_is_twenty_seconds(self) -> None:
        self.assertEqual(actions_module.HUMAN_TYPING_BUDGET_SECONDS, 20.0)


if __name__ == "__main__":
    unittest.main()
