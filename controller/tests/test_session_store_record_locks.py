"""DurableSessionStore serializes writes per record id, not across the store (CB3-2).

One store-wide lock made every session's persist (each action persists) queue behind one slow write, up to Redis's
2 s socket timeout. The lock is per id now; the closed-record guard and the run-to-completion hold per id as before.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from app.models import SessionRecord
from app.session_store import DurableSessionStore


def _record(session_id: str, status: str) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        name=session_id,
        created_at="2026-01-01T00:00:01Z",
        updated_at="2026-01-01T00:00:00Z",
        status=status,  # type: ignore[arg-type]
        live=status == "active",
        current_url="https://example.com",
        title="Example",
        artifact_dir="/tmp/artifacts",
        takeover_url="http://localhost:6080",
        remote_access={},
        isolation={},
    )


class RecordLockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DurableSessionStore(file_root=Path(self.tmp.name) / "sessions", redis_url=None, redis_prefix="t")
        await self.store.startup()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)  # never leave a worker thread parked
        self.landed: list[tuple[str, str]] = []
        real = self.store.file_store._upsert_sync

        def gated(record: SessionRecord) -> None:
            if record.id == "slow" and record.status == "active":
                self.entered.set()
                self.release.wait(10)
            real(record)
            self.landed.append((record.id, record.status))

        self.store.file_store._upsert_sync = gated  # type: ignore[method-assign]

    async def wait_entered(self) -> None:
        await asyncio.wait_for(asyncio.to_thread(self.entered.wait, 10), timeout=11)

    async def test_a_slow_write_for_one_id_does_not_delay_another(self) -> None:
        slow = asyncio.create_task(self.store.upsert(_record("slow", "active")))
        await self.wait_entered()

        await asyncio.wait_for(self.store.upsert(_record("fast", "active")), timeout=5)

        self.assertEqual(self.landed, [("fast", "active")])
        self.release.set()
        await slow
        self.assertEqual(self.landed, [("fast", "active"), ("slow", "active")])

    async def test_a_cancelled_write_finishes_first_and_a_closed_write_of_the_same_id_waits_for_it(self) -> None:
        active = asyncio.create_task(self.store.upsert(_record("slow", "active")))
        await self.wait_entered()
        closed = asyncio.create_task(self.store.upsert(_record("slow", "closed")))
        await asyncio.sleep(0.05)
        active.cancel()
        await asyncio.sleep(0.05)

        # The cancel waits for the write in flight, and the closed write waits behind it.
        self.assertFalse(active.done())
        self.assertFalse(closed.done())
        self.assertEqual(self.landed, [])

        self.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await active
        await closed
        self.assertEqual(self.landed, [("slow", "active"), ("slow", "closed")])
        self.assertEqual((await self.store.get("slow")).status, "closed")

    async def test_the_lock_table_is_empty_once_the_writes_finish(self) -> None:
        self.release.set()
        await asyncio.gather(*(self.store.upsert(_record(f"s{index % 3}", "active")) for index in range(9)))
        await self.store.upsert(_record("slow", "active"))

        self.assertEqual(self.store._write_locks, {})

    async def test_a_closed_record_is_still_not_reopened(self) -> None:
        self.release.set()
        await self.store.upsert(_record("s1", "closed"))
        await self.store.upsert(_record("s1", "active"))

        self.assertEqual((await self.store.get("s1")).status, "closed")
        self.assertEqual(self.store._write_locks, {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
