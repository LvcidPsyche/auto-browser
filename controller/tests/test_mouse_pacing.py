"""The human-like mouse path paces its moves by the gap between them, not the
gap on top of however long each move took to dispatch.

The clock is driven by the test: real sleeps measured against a real clock made
these depend on the host (Windows' 15.6 ms monotonic tick, a loaded machine).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.browser.services.actions import BrowserActionService


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _service_and_session(move) -> tuple[BrowserActionService, SimpleNamespace]:
    manager = SimpleNamespace(settings=SimpleNamespace(default_viewport_width=1280, default_viewport_height=720))
    session = SimpleNamespace(page=SimpleNamespace(mouse=SimpleNamespace(move=move)), mouse_position=(10.0, 10.0))
    return BrowserActionService(manager), session


class MousePacingTests(unittest.IsolatedAsyncioTestCase):
    async def _move(self, move_seconds: float) -> tuple[AsyncMock, AsyncMock, SimpleNamespace]:
        clock = FakeClock()

        async def move(x: float, y: float) -> None:
            clock.now += move_seconds

        mover = AsyncMock(side_effect=move)
        service, session = _service_and_session(mover)
        with (
            patch("app.browser.services.actions._step_clock", clock),
            patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            await service.move_mouse_human_like(session, 400.0, 300.0)
        return mover, sleep, session

    async def test_a_slow_move_uses_up_the_gap(self) -> None:
        _mover, sleep, session = await self._move(0.02)  # longer than the widest 18 ms gap
        sleep.assert_not_awaited()
        self.assertEqual(session.mouse_position, (400.0, 300.0))

    async def test_an_instant_move_still_waits_out_the_gap(self) -> None:
        mover, sleep, _session = await self._move(0.0)
        self.assertEqual(sleep.await_count, mover.await_count)
        for call in sleep.await_args_list:
            self.assertTrue(0 < call.args[0] <= 0.018, call.args[0])

    async def test_only_what_is_left_of_the_gap_is_slept(self) -> None:
        _mover, sleep, _session = await self._move(0.003)  # shorter than the narrowest 4 ms gap
        for call in sleep.await_args_list:
            self.assertTrue(0 < call.args[0] <= 0.015 + 1e-9, call.args[0])
