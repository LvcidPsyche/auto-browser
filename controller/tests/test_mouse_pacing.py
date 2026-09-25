"""The human-like mouse path paces its moves by the gap between them, not the
gap on top of however long each move took to dispatch."""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.browser.services.actions import BrowserActionService


def _service_and_session(move) -> tuple[BrowserActionService, SimpleNamespace]:
    manager = SimpleNamespace(settings=SimpleNamespace(default_viewport_width=1280, default_viewport_height=720))
    session = SimpleNamespace(page=SimpleNamespace(mouse=SimpleNamespace(move=move)), mouse_position=(10.0, 10.0))
    return BrowserActionService(manager), session


class MousePacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_slow_move_uses_up_the_gap(self) -> None:
        async def slow_move(x: float, y: float) -> None:
            time.sleep(0.02)  # longer than the widest 18 ms gap

        service, session = _service_and_session(slow_move)
        with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()) as sleep:
            await service.move_mouse_human_like(session, 400.0, 300.0)
        sleep.assert_not_awaited()
        self.assertEqual(session.mouse_position, (400.0, 300.0))

    async def test_an_instant_move_still_waits_out_the_gap(self) -> None:
        move = AsyncMock()
        service, session = _service_and_session(move)
        with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()) as sleep:
            await service.move_mouse_human_like(session, 400.0, 300.0)
        self.assertEqual(sleep.await_count, move.await_count)
        for call in sleep.await_args_list:
            self.assertTrue(0 < call.args[0] <= 0.018, call.args[0])
