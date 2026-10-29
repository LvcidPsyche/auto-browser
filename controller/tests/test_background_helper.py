"""The test helpers that wait for background tasks wait only for the current test's."""

from __future__ import annotations

import asyncio

from app.utils import spawn_background_task
from tests import _background
from tests.test_allowed_hosts_rechecks import drain


async def test_drain_does_not_wait_for_a_task_an_earlier_test_left_pending() -> None:
    leftover = spawn_background_task(asyncio.Event().wait())
    _background.mark()  # where the next test would start
    try:
        await drain(timeout=0.5)
        assert _background.pending() == set()
    finally:
        leftover.cancel()
        await asyncio.gather(leftover, return_exceptions=True)


async def test_control_drain_waits_for_the_current_tests_own_task() -> None:
    finished: list[bool] = []

    async def later() -> None:
        await asyncio.sleep(0.1)
        finished.append(True)

    spawn_background_task(later())
    await drain(timeout=2.0)

    assert finished == [True]
