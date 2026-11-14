from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from .utils import error_text_for_log

logger = logging.getLogger(__name__)

# PRAGMA synchronous takes no bound parameter, so the value is checked against SQLite's own before it is written in.
_SYNCHRONOUS_MODES = frozenset({"OFF", "NORMAL", "FULL", "EXTRA"})


def connect_sqlite(
    db_path: str | Path,
    *,
    timeout: float = 10.0,
    row_factory: Any | None = None,
    synchronous: str = "NORMAL",
) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=timeout)
    try:
        if row_factory is not None:
            conn.row_factory = row_factory
        configure_sqlite_connection(conn, db_path=db_path, timeout=timeout, synchronous=synchronous)
    except BaseException:
        # Not left to the garbage collector (STORE-38): an open connection holds its file, locked on Windows.
        conn.close()
        raise
    return conn


def configure_sqlite_connection(
    conn: sqlite3.Connection,
    *,
    db_path: str | Path | None = None,
    timeout: float = 10.0,
    synchronous: str = "NORMAL",
) -> None:
    """Busy timeout, WAL, and ``synchronous``: NORMAL, or FULL for a database whose every commit must survive a
    power loss (with WAL and NORMAL, the last commits can roll back)."""
    if synchronous not in _SYNCHRONOUS_MODES:
        raise ValueError(f"synchronous must be one of {', '.join(sorted(_SYNCHRONOUS_MODES))}, got {synchronous!r}")
    busy_timeout_ms = max(1, int(timeout * 1000))
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    except sqlite3.DatabaseError as exc:
        logger.warning("sqlite WAL unavailable for %s: %s", db_path or "<memory>", error_text_for_log(exc))
    else:
        if mode is not None and str(mode[0]).lower() != "wal":
            logger.warning("sqlite WAL not enabled for %s: journal_mode=%s", db_path or "<memory>", mode[0])
    conn.execute(f"PRAGMA synchronous={synchronous}")
