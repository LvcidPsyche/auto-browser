"""A test leaves no background task behind (round 4c, F6).

tests/conftest.py gives the tasks a test started (app.utils.spawn_background_task) up to
_background.SETTLE_SECONDS after it, on their own event loop, then cancels the rest and fails the test with
their names: a page guard still waiting for a lock, say, which a later test would otherwise inherit.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.utils import spawn_background_task
from tests import _background, conftest

SHORT_SETTLE_SECONDS = 0.3


async def _hangs() -> None:
    await asyncio.Event().wait()


async def _finishes() -> None:
    await asyncio.sleep(0.05)


def test_a_task_that_finishes_in_time_is_waited_for_and_one_that_does_not_is_cancelled_and_named() -> None:
    loop = asyncio.new_event_loop()
    try:
        _background.mark()

        async def start() -> list[asyncio.Task]:
            return [spawn_background_task(_hangs()), spawn_background_task(_finishes())]

        hangs, finishes = loop.run_until_complete(start())

        left = _background.settle(SHORT_SETTLE_SECONDS)

        assert left == ["_hangs"]
        assert hangs.cancelled() and finishes.done() and not finishes.cancelled()
        assert _background.pending() == set()
    finally:
        loop.close()


def test_control_no_task_left_names_nothing() -> None:
    _background.mark()

    assert _background.settle(SHORT_SETTLE_SECONDS) == []


# ── The conftest fixture fails the test that left a task (round 4d, T5) ──


async def _raises() -> None:
    raise AttributeError("'EventPage' object has no attribute 'goto'")


def through_the_fixture(body) -> str | None:
    """Run ``body`` (a coroutine function) as a test between conftest's fixture's setup and teardown, on a
    loop of its own that stays open until after the teardown, as an asyncio test's does. The teardown's
    failure message, or None when the test passed."""
    loop = asyncio.new_event_loop()
    fixture = conftest.leaves_no_background_tasks(SimpleNamespace(node=None))
    try:
        next(fixture)
        loop.run_until_complete(body())
        try:
            next(fixture)
        except StopIteration:
            return None
        except pytest.fail.Exception as failed:
            return str(failed)
        raise AssertionError("the fixture yielded twice")
    finally:
        loop.close()


@pytest.mark.parametrize(
    ("task", "named"), [(_hangs, "_hangs"), (_raises, "_raises (raised AttributeError")], ids=["leaked", "crashed"]
)
def test_a_task_a_test_leaked_or_that_failed_fails_that_test_by_name(monkeypatch, task, named: str) -> None:
    """A task that failed is done, so settle() neither waited for it nor named it: a page guard that raised
    left a log line, and the test passed. Started as the test's last step, it ends in the loop's last turn,
    before its failure is reported."""
    monkeypatch.setattr(_background, "SETTLE_SECONDS", SHORT_SETTLE_SECONDS)

    async def body() -> None:
        spawn_background_task(task())

    failure = through_the_fixture(body)

    assert failure is not None and named in failure, failure


def test_control_a_test_whose_tasks_finished_passes(monkeypatch) -> None:
    monkeypatch.setattr(_background, "SETTLE_SECONDS", SHORT_SETTLE_SECONDS)

    async def body() -> None:
        spawn_background_task(_finishes())

    assert through_the_fixture(body) is None


def test_a_failure_the_test_expected_does_not_fail_it(monkeypatch) -> None:
    monkeypatch.setattr(_background, "SETTLE_SECONDS", SHORT_SETTLE_SECONDS)

    async def body() -> None:
        with _background.crashes_expected():
            await asyncio.wait([spawn_background_task(_raises())])
            await asyncio.sleep(SHORT_SETTLE_SECONDS / 10)

    assert through_the_fixture(body) is None
