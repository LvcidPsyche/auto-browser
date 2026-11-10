"""One stored time that cannot be read holds its own record back, and nothing else.

A rejected tool call's arguments go once the rejection is older than the approval TTL, in a sweep that runs at a start
and inside every request for an approval; and a start compares SQLite's record of a rejection with the file store's by
their times. Each read a stored time with nothing caught: a `decided_at` that is not a time failed the start and every
request for an approval while the record existed, and an `updated_at` that is not one left the whole store on the file
records, where the rejection it was deciding about read approved.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.approvals import ApprovalStore
from app.models import BrowserActionDecision
from app.utils import UTC

SESSION = "abc123abc123"
NOT_A_TIME = "the day before yesterday"
# No offset, so it is read as local time: Windows raises OSError for one before 1970.
BEFORE_1970 = "1960-01-01T00:00:00"


def _ago(**age: float) -> str:
    return (datetime.now(UTC) - timedelta(**age)).isoformat().replace("+00:00", "Z")


def _as_windows_reads_it():
    """_parse_timestamp as it fails on Windows for BEFORE_1970, on every platform."""
    parse = ApprovalStore._parse_timestamp

    def parse_as_windows(value: str) -> datetime:
        if value == BEFORE_1970:
            raise OSError(22, "Invalid argument")
        return parse(value)

    return patch.object(ApprovalStore, "_parse_timestamp", staticmethod(parse_as_windows))


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "approvals"
        self.db_path = Path(self.tmp.name) / "state.db"
        self._seeded = 0
        self.store = await self._started()

    async def _started(self) -> ApprovalStore:
        store = ApprovalStore(self.root, db_path=str(self.db_path))
        await store.startup()
        return store

    async def _pending(self) -> str:
        self._seeded += 1
        action = BrowserActionDecision(
            action="click", reason="pay", selector=f"#pay-{self._seeded}", risk_category="payment"
        )
        approval = await self.store.create_or_reuse_pending(
            session_id=SESSION, kind="payment", reason="r", action=action
        )
        return approval.id


class ArgumentsSweepTests(_Base):
    async def _rejected_with_arguments(self, decided_at: str) -> str:
        """A rejected approval that still has its tool call's arguments, its decision stamped ``decided_at``."""
        approval_id = await self._pending()
        await self.store.file_store.write_arguments(approval_id, "{}")
        await self.store.reject(approval_id)
        record = await self.store.get(approval_id)
        record.decided_at = decided_at
        await self.store._persist(record)
        return approval_id

    async def _kept(self) -> set[str]:
        return set(await self.store.file_store.arguments_ids())

    async def test_a_request_for_an_approval_goes_on_past_a_decision_time_that_cannot_be_read(self) -> None:
        for unreadable in (NOT_A_TIME, BEFORE_1970):
            with self.subTest(decided_at=unreadable), _as_windows_reads_it():
                # Every request for an approval below runs the sweep: those that make the other records too.
                with self.assertLogs("app.approvals", level="WARNING") as logged:
                    garbled = await self._rejected_with_arguments(unreadable)
                    lapsed = await self._rejected_with_arguments(_ago(days=2))
                    fresh = await self._rejected_with_arguments(_ago(seconds=5))
                    await self._pending()
                    await self._pending()

                kept = await self._kept()
                self.assertIn(garbled, kept, "the arguments of a record whose age cannot be told were removed")
                self.assertIn(fresh, kept)
                self.assertNotIn(lapsed, kept, "the sweep stopped at the record it could not read")
                self.assertEqual(sum(garbled in line for line in logged.output), 1, logged.output)

    async def test_a_start_goes_on_past_a_decision_time_that_cannot_be_read(self) -> None:
        for unreadable in (NOT_A_TIME, BEFORE_1970):
            with self.subTest(decided_at=unreadable), _as_windows_reads_it():
                garbled = await self._rejected_with_arguments(unreadable)
                lapsed = await self._rejected_with_arguments(_ago(days=2))

                with self.assertLogs("app.approvals", level="WARNING") as logged:
                    self.store = await self._started()

                kept = await self._kept()
                self.assertIn(garbled, kept)
                self.assertNotIn(lapsed, kept)
                self.assertEqual(sum(garbled in line for line in logged.output), 1, logged.output)
                self.assertEqual((await self.store.get(garbled)).status, "rejected")


class CatchUpTests(_Base):
    """At a start SQLite's rejection stays over a file record not later than it: with a time it cannot read, too."""

    async def _rejected_in_sqlite_and_approved_on_file(self, *, unreadable_in: str, value: str) -> str:
        approval_id = await self._pending()
        approved = await self.store.approve(approval_id)
        rejected = await self.store.reject(approval_id, comment="no")
        if unreadable_in == "file":
            approved.updated_at = value
        else:
            rejected.updated_at = value
            await self.store.sqlite_store.upsert(rejected)
        await self.store.file_store.upsert(approved)  # the two stores now differ: approved here, rejected in SQLite
        return approval_id

    async def test_a_file_record_with_a_time_that_cannot_be_read_does_not_undo_sqlites_rejection(self) -> None:
        cases = [(where, value) for where in ("file", "sqlite") for value in (NOT_A_TIME, BEFORE_1970)]
        for where, value in cases:
            with self.subTest(unreadable_in=where, updated_at=value), _as_windows_reads_it():
                approval_id = await self._rejected_in_sqlite_and_approved_on_file(unreadable_in=where, value=value)
                other = await self._pending()

                with self.assertLogs("app.approvals", level="WARNING") as logged:
                    back = await self._started()

                self.assertIs(back._primary, back.sqlite_store, "one record left the whole store on the file records")
                self.assertEqual((await back.get(approval_id)).status, "rejected")
                self.assertEqual((await back.file_store.get(approval_id)).status, "rejected")
                with self.assertRaises(PermissionError):
                    await back.claim_execution(approval_id)
                self.assertEqual((await back.get(other)).status, "pending")
                self.assertTrue(
                    any(approval_id in line and "cannot be read" in line for line in logged.output), logged.output
                )
                self.store = back


if __name__ == "__main__":
    unittest.main()
