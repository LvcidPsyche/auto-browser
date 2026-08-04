from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

UTC = timezone.utc

# Strong references to fire-and-forget background tasks. The event loop only keeps
# a weak reference to a task, so a task scheduled without a saved reference can be
# garbage-collected before it finishes ("Task was destroyed but it is pending").
# Tasks remove themselves from this set on completion.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def utc_now() -> str:
    """Return current UTC timestamp as ISO-8601 string with Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def spawn_background_task(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Schedule a fire-and-forget coroutine while retaining a strong reference.

    Use instead of a bare ``asyncio.ensure_future``/``create_task`` whenever the
    result is not awaited, so the task cannot be collected mid-execution.
    """
    task = asyncio.ensure_future(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    task.add_done_callback(_log_background_task_exception)
    return task


def _log_background_task_exception(task: asyncio.Task[Any]) -> None:
    """Surface failures in fire-and-forget tasks.

    Nothing awaits these, so without this an exception only ever appeared as
    asyncio's "exception was never retrieved" warning at garbage-collection
    time — long after the fact, unattributed, and easy to miss entirely.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("background task %s failed: %s", task.get_name(), exc, exc_info=exc)
