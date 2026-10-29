"""The background tasks (app.utils.spawn_background_task) the current test started.

The helpers that wait for the page guard and the network inspector waited for every task in the
process-wide set, so a task an earlier test left pending (on a loop that has closed, or a guard still
waiting for a lock) stalled a later test or failed it. They wait only for tasks started since the
current test began, which tests/conftest.py marks. After the test, conftest.py gives those tasks
SETTLE_SECONDS to finish (settle), then cancels the rest and fails the test with their names, and with
the names of those that failed (a page guard that raised, say), unless the test expected it
(crashes_expected).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterator
from typing import Any

from app.utils import _BACKGROUND_TASKS

# How long a test's background tasks may run on after it before they are cancelled and fail it.
SETTLE_SECONDS = 2.0
# The logger and function app.utils reports a failed background task through.
_FAILURE_LOGGER = "app.utils"
_FAILURE_REPORTER = "_log_background_task_exception"

_before: frozenset[asyncio.Task[Any]] = frozenset()


class _Failures(logging.Handler):
    """Background tasks that ended with an exception, as app.utils reports them. A task leaves
    _BACKGROUND_TASKS as it ends, so this report is all that is left of it by the time the test ends."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.names: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.funcName != _FAILURE_REPORTER or not record.exc_info:
            return
        _type, exc, traceback = record.exc_info
        # The first frame of a task's traceback is its coroutine's.
        where = traceback.tb_frame.f_code.co_qualname if traceback is not None else record.getMessage()
        self.names.append(f"{where} (raised {type(exc).__name__}: {exc})")


_FAILURES = _Failures()
logging.getLogger(_FAILURE_LOGGER).addHandler(_FAILURES)


def mark() -> None:
    """Called as each test starts: tasks pending now are an earlier test's."""
    global _before
    _before = frozenset(_BACKGROUND_TASKS)
    _FAILURES.names.clear()


def pending() -> set[asyncio.Task[Any]]:
    """Tasks the current test started that have not finished."""
    return set(_BACKGROUND_TASKS) - _before


@contextlib.contextmanager
def crashes_expected() -> Iterator[None]:
    """For a test that makes a background task fail on purpose: failures inside the block do not fail it."""
    kept = len(_FAILURES.names)
    try:
        yield
    finally:
        del _FAILURES.names[kept:]


def settle(timeout: float) -> list[str]:
    """Give the current test's tasks up to ``timeout`` seconds, cancel the rest, and return their names,
    then those of its tasks that failed, with their errors.

    Called between tests, when no event loop runs: each task's own loop is run here until its tasks
    finish or the time is up. A task whose loop has closed cannot run or be cancelled any more; it is
    named all the same.
    """
    deadline = time.monotonic() + timeout
    # A task that ended in the loop's last turn has not run its done callbacks: app.utils reports its
    # failure only when its loop runs again (pytest-asyncio's teardown), after this. One turn runs them.
    for loop, _ended in _by_open_loop([task for task in pending() if task.done()]):
        loop.run_until_complete(asyncio.sleep(0))
    left = [task for task in pending() if not task.done()]
    for loop, tasks in _by_open_loop(left):
        loop.run_until_complete(asyncio.wait(tasks, timeout=max(0.0, deadline - time.monotonic())))
    left = [task for task in left if not task.done()]
    names = sorted(_name(task) for task in left)
    for loop, tasks in _by_open_loop(left):
        for task in tasks:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    failed = sorted(_FAILURES.names)
    _FAILURES.names.clear()
    return names + failed


def _by_open_loop(tasks: list[asyncio.Task[Any]]) -> list[tuple[asyncio.AbstractEventLoop, list[asyncio.Task[Any]]]]:
    loops = {task.get_loop() for task in tasks}
    return [
        (loop, [task for task in tasks if task.get_loop() is loop])
        for loop in loops
        if not loop.is_closed() and not loop.is_running()
    ]


def _name(task: asyncio.Task[Any]) -> str:
    coro = task.get_coro()
    return getattr(coro, "__qualname__", None) or repr(task)
