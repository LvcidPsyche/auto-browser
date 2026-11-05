"""A session's records (an action's or a takeover's `actions.jsonl` line, audit entry and receipt), written as one unit
that a cancellation does not cut short, and in order.

A cancellation that lands while a record is written goes on at once, and the writes finish behind it in a task. The
session keeps that task (`pending_record`) until it is done, so what records next on the session (its next action,
its close, a takeover) waits for it first, and the controller's shutdown waits for every one still running.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Longest what records next on a session waits for the record before it, and longest shutdown waits for them all.
RECORD_WAIT_SECONDS = 5.0
# Records still being written: held, as the event loop holds a task only weakly.
_UNFINISHED_RECORDS: set[asyncio.Task[None]] = set()


async def write_record(session: Any, writes: Callable[[], Awaitable[None]]) -> None:
    """Run ``writes`` after the session's pending record, as a task a cancellation of the caller does not cut short.

    Each store orders its own writes (the audit's and the witness chain's locks; actions.jsonl takes one appended
    line), so writes that land after the caller has gone on are still whole.
    """
    previous = getattr(session, "pending_record", None)

    async def in_order() -> None:
        await _wait_for(previous, session)
        await writes()

    task = asyncio.ensure_future(in_order())
    _UNFINISHED_RECORDS.add(task)
    task.add_done_callback(_UNFINISHED_RECORDS.discard)
    session.pending_record = task
    task.add_done_callback(lambda done: _forget(session, done))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_log_unfinished_record)
        raise


async def wait_for_pending_record(session: Any) -> None:
    """Let the session's record still being written (behind a cancelled action) finish first, for at most
    RECORD_WAIT_SECONDS. A record that ended in an error or cancelled is finished: neither is raised here."""
    await _wait_for(getattr(session, "pending_record", None), session)


async def drain_unfinished_records(timeout: float = RECORD_WAIT_SECONDS) -> None:
    """At shutdown: wait, ``timeout`` in all, for the records still being written, and log any left unfinished."""
    loop = asyncio.get_running_loop()
    pending = {task for task in _UNFINISHED_RECORDS if task.get_loop() is loop and not task.done()}
    if not pending:
        return
    _, left = await asyncio.wait(pending, timeout=timeout)
    if left:
        logger.warning(
            "%d action record(s) were still being written after %.0f s at shutdown; they may be cut short",
            len(left),
            timeout,
        )


async def _wait_for(pending: asyncio.Task[None] | None, session: Any) -> None:
    if pending is None or pending.done():
        return
    # asyncio.wait never raises the task's error or cancellation, and never cancels it.
    done, _ = await asyncio.wait({pending}, timeout=RECORD_WAIT_SECONDS)
    if pending not in done:
        logger.warning(
            "session %s: the previous record was still being written after %.0f s; going on",
            getattr(session, "id", "?"),
            RECORD_WAIT_SECONDS,
        )


def _forget(session: Any, task: asyncio.Task[None]) -> None:
    if getattr(session, "pending_record", None) is task:
        session.pending_record = None


def _log_unfinished_record(task: asyncio.Task[None]) -> None:
    """A record whose writes failed after its caller was cancelled: nothing else would say so."""
    if not task.cancelled() and task.exception() is not None:
        logger.warning("a record failed after the action that wrote it was cancelled", exc_info=task.exception())
