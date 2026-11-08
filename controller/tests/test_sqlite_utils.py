from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.audit import SQLiteAuditStore
from app.sqlite_utils import configure_sqlite_connection, connect_sqlite

# PRAGMA synchronous reads back as a number.
NORMAL, FULL = 1, 2


class SQLiteUtilsTests(unittest.TestCase):
    def test_connect_sqlite_applies_operational_pragmas(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "state.db"
            conn = connect_sqlite(db_path, timeout=7)
            try:
                journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(str(journal_mode).lower(), "wal")
            self.assertEqual(busy_timeout, 7000)
            self.assertEqual(synchronous, NORMAL)


class SQLiteDurabilityTests(unittest.TestCase):
    """STORE-38: WAL not taking was a debug line, every database synced as NORMAL, and a connection whose
    configuration failed was left open."""

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)

    def _synchronous(self, conn: sqlite3.Connection) -> int:
        try:
            return conn.execute("PRAGMA synchronous").fetchone()[0]
        finally:
            conn.close()

    def test_a_database_that_does_not_take_wal_is_a_warning(self) -> None:
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("delete",)

        with self.assertLogs("app.sqlite_utils", level="WARNING") as logs:
            configure_sqlite_connection(conn, db_path="state.db")

        self.assertIn("journal_mode=delete", logs.output[0])

    def test_synchronous_is_normal_unless_full_is_asked_for(self) -> None:
        self.assertEqual(self._synchronous(connect_sqlite(self.root / "a.db")), NORMAL)
        self.assertEqual(self._synchronous(connect_sqlite(self.root / "b.db", synchronous="FULL")), FULL)

    def test_the_audit_database_syncs_every_commit(self) -> None:
        # With WAL and NORMAL, the last commits can roll back after a power loss: an audit event must not.
        store = SQLiteAuditStore(self.root / "audit.db", max_events=10)

        self.assertEqual(self._synchronous(store._connect()), FULL)

    def test_a_synchronous_value_that_is_no_mode_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "synchronous"):
            connect_sqlite(self.root / "c.db", synchronous="FULL; DROP TABLE events")

    def test_a_connection_whose_configuration_fails_is_closed(self) -> None:
        opened: list[sqlite3.Connection] = []
        self.addCleanup(lambda: [conn.close() for conn in opened])
        real_connect = sqlite3.connect

        def connect(*args, **kwargs) -> sqlite3.Connection:
            opened.append(real_connect(*args, **kwargs))
            return opened[-1]

        failing = sqlite3.OperationalError("database is locked")
        with (
            patch("app.sqlite_utils.sqlite3.connect", side_effect=connect),
            patch("app.sqlite_utils.configure_sqlite_connection", side_effect=failing),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                connect_sqlite(self.root / "d.db")

        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
