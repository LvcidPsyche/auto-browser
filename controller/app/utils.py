from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import tempfile
import time
from collections.abc import Coroutine
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

UTC = timezone.utc

# Strong references to fire-and-forget background tasks. The event loop only keeps
# a weak reference to a task, so a task scheduled without a saved reference can be
# garbage-collected before it finishes ("Task was destroyed but it is pending").
# Tasks remove themselves from this set on completion.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


# mkstemp creates 0600 files; atomic_write_text gives the file the mode it should
# have (see _target_mode) before it replaces the target.
_UMASK = os.umask(0)
os.umask(_UMASK)

# What stores that hold credentials, tokens or typed text ask atomic_write_text for.
PRIVATE_FILE_MODE = 0o600

_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def record_path(root: Path, record_id: str, suffix: str) -> Path:
    """The file a file-backed store keeps ``record_id`` in, for one plain name only.

    Record ids reach the stores from MCP tool arguments, which — unlike REST path
    segments — may contain "/" and "..". Joined into the store root unchecked, an
    id such as "../audit/events" read a file outside it. Raised as KeyError
    because, to every caller, an id that cannot name a record names no record.
    """
    if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
        raise KeyError(record_id)
    return root / f"{record_id}{suffix}"


def _target_mode(path: Path, mode: int | None) -> int:
    """The permission bits the replacement of ``path`` gets.

    An explicit ``mode`` wins, so a store that holds secrets can ask for 0600 and
    tighten a file an older release left wider. Otherwise the file keeps the mode
    it has (a 0600 file must not become 0644 because it was saved again), and a
    file that does not exist yet gets what a plain open() would have made.
    """
    if mode is not None:
        return mode & 0o777
    try:
        return stat.S_IMODE(os.stat(path).st_mode) & 0o777
    except FileNotFoundError:
        return 0o666 & ~_UMASK


# Windows refuses a rename while another process has the target open (an
# antivirus scan, an indexer, a reader thread); that clears within milliseconds.
RENAME_ATTEMPTS = 5
RENAME_RETRY_STEP_SECONDS = 0.01


def replace_retrying(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    """os.replace, retried briefly through a sharing violation (PermissionError), then raised."""
    for attempt in range(RENAME_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == RENAME_ATTEMPTS - 1:
                raise
            time.sleep(RENAME_RETRY_STEP_SECONDS * (attempt + 1))


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Replace ``path`` with ``text`` so readers only ever see a whole file.

    Each call writes to its own temp file in the target directory. The stores
    used to share one fixed ``<name>.json.tmp`` per record, so two concurrent
    saves of the same record (they run in worker threads) raced: one writer's
    rename moved the other's temp file away, failing it with FileNotFoundError,
    or both wrote into the same temp file and a torn mix of the two was renamed
    into place. The temp name keeps a ``.tmp`` suffix so ``*.json`` globs skip it.

    ``mode`` sets the file's permission bits (pass ``PRIVATE_FILE_MODE`` for
    anything that holds a credential, token or typed text). Without it the
    file keeps the mode it already has.
    """
    target_mode = _target_mode(path, mode)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, target_mode)
        replace_retrying(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


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
