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


async def test_a_background_task_that_failed_is_named_among_the_failures() -> None:
    # What fails a test whose task raised: app.utils reports it with no exc_info, by its text and its frames.
    async def raises() -> None:
        raise RuntimeError("the guard failed")

    with _background.crashes_expected():
        task = spawn_background_task(raises())
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # the done callbacks run in the loop's next turn

        (named,) = _background._FAILURES.names
    assert named == "raises (raised RuntimeError: 'the guard failed')", named
