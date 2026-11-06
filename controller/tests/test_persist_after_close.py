"""A background persist never moves a closed session back to active (STORE-44)."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.models import SessionRecord
from app.session_ownership import acting_as
from app.session_store import DurableSessionStore
from tests.test_auth_state_ownership import ALICE, build_manager


def _record(status: str, *, live: bool) -> SessionRecord:
    return SessionRecord(
        id="s1",
        name="s1",
        created_at="2026-01-01T00:00:01Z",
        updated_at="2026-01-01T00:00:00Z",
        status=status,  # type: ignore[arg-type]
        live=live,
        current_url="https://example.com",
        title="Example",
        artifact_dir="/tmp/artifacts",
        takeover_url="http://localhost:6080",
        remote_access={},
    )


class PersistAfterCloseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager, _ = build_manager(Path(self.tmp.name))
        self.manager.settings.max_sessions = 5
        with acting_as(ALICE):
            self.session_id = (await self.manager.create_session(name="alice"))["id"]
        self.session = self.manager.sessions[self.session_id]

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def stored_status(self) -> str:
        return (await self.manager.session_store.get(self.session_id)).status

    async def test_a_persist_after_the_close_leaves_the_record_closed(self) -> None:
        with acting_as(ALICE):
            await self.manager.close_session(self.session_id)

        await self.manager._persist_session(self.session, status="active")

        self.assertEqual(await self.stored_status(), "closed")

    async def test_a_session_closed_while_the_summary_is_built_is_not_persisted_active(self) -> None:
        real_summary = self.manager._session_summary

        async def summary_then_close(session, **kwargs):
            summary = await real_summary(session, **kwargs)
            # Once: the close builds its own summary through this same method.
            self.manager._session_summary = real_summary  # type: ignore[method-assign]
            with acting_as(ALICE):
                await self.manager.close_session(self.session_id)
            return summary

        self.manager._session_summary = summary_then_close  # type: ignore[method-assign]
        await self.manager._persist_session(self.session, status="active")

        self.assertEqual(await self.stored_status(), "closed")

    async def test_b_fix_4_persist_attempts_no_active_write_after_the_close(self) -> None:
        # The store's closed refusal masked persist()'s own registration checks: with upsert recording every write,
        # an active write must not even be attempted.
        with acting_as(ALICE):
            await self.manager.close_session(self.session_id)
        upsert = AsyncMock()
        self.manager.session_store.upsert = upsert  # type: ignore[method-assign]
        # B3-5: the second check (after the summary) also stops the write, so only this pins the first one.
        summary = AsyncMock(wraps=self.manager._session_summary)
        self.manager._session_summary = summary  # type: ignore[method-assign]

        await self.manager._persist_session(self.session, status="active")

        summary.assert_not_awaited()
        upsert.assert_not_awaited()

    async def test_b_fix_4_persist_attempts_no_active_write_when_the_close_races_the_summary(self) -> None:
        real_summary = self.manager._session_summary
        upsert = AsyncMock(wraps=self.manager.session_store.upsert)

        async def summary_then_close(session, **kwargs):
            summary = await real_summary(session, **kwargs)
            self.manager._session_summary = real_summary  # type: ignore[method-assign]
            with acting_as(ALICE):
                await self.manager.close_session(self.session_id)
            self.manager.session_store.upsert = upsert  # type: ignore[method-assign]  # from here on, recorded
            return summary

        self.manager._session_summary = summary_then_close  # type: ignore[method-assign]
        await self.manager._persist_session(self.session, status="active")

        self.assertEqual([call.args[0].status for call in upsert.await_args_list], [])

    async def test_a_live_session_still_persists_active(self) -> None:
        self.session.last_action = "click"

        await self.manager._persist_session(self.session, status="active")

        self.assertEqual(await self.stored_status(), "active")

    async def test_the_store_refuses_to_reopen_a_closed_record(self) -> None:
        store = DurableSessionStore(file_root=Path(self.tmp.name) / "rec", redis_url=None, redis_prefix="t")
        await store.startup()
        await store.upsert(_record("closed", live=False))

        await store.upsert(_record("active", live=True))

        self.assertEqual((await store.get("s1")).status, "closed")

    async def _store(self) -> DurableSessionStore:
        store = DurableSessionStore(file_root=Path(self.tmp.name) / "rec", redis_url=None, redis_prefix="t")
        await store.startup()
        return store

    async def test_an_active_write_whose_check_ran_before_a_close_does_not_land_after_it(self) -> None:
        # R-13 (a): the closed check and the write ran in separate thread hops with no lock, so an active write that
        # had read the record before a close landed after it and reopened the session.
        store = await self._store()
        await store.upsert(_record("active", live=True))
        read_done, release = threading.Event(), threading.Event()
        real_get = store.file_store._get_sync
        gated = [True]

        def gated_get(session_id: str) -> SessionRecord:
            result = real_get(session_id)
            if gated[0]:
                gated[0] = False
                read_done.set()
                release.wait(5)
            return result

        store.file_store._get_sync = gated_get  # type: ignore[method-assign]
        active = asyncio.create_task(store.upsert(_record("active", live=True)))
        await asyncio.to_thread(read_done.wait, 5)
        closed_record = _record("closed", live=False).model_copy(update={"close_reason": "operator"})
        closed = asyncio.create_task(store.upsert(closed_record))
        for _ in range(50):  # the close lands, or waits behind the active write
            if closed.done():
                break
            await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(active, closed)

        stored = await store.get("s1")
        self.assertEqual(stored.status, "closed")
        self.assertEqual(stored.close_reason, "operator")

    async def test_a_slower_redis_active_write_cannot_land_after_the_close(self) -> None:
        # R-13 (b): get() and list() prefer the Redis copy, which an out-of-order active write left active.
        store = await self._store()

        class _SlowActiveRedis:
            def __init__(self) -> None:
                self.records: dict[str, SessionRecord] = {}

            async def upsert(self, record: SessionRecord) -> None:
                if record.status == "active":
                    await asyncio.sleep(0.1)
                self.records[record.id] = record

            async def get(self, session_id: str) -> SessionRecord:
                return self.records[session_id]

        redis = _SlowActiveRedis()
        store.redis_store = redis  # type: ignore[assignment]
        store._primary = redis  # type: ignore[assignment]

        await asyncio.gather(store.upsert(_record("active", live=True)), store.upsert(_record("closed", live=False)))

        self.assertEqual((await store.get("s1")).status, "closed")

    async def test_an_interrupted_write_does_not_replace_a_closed_record(self) -> None:
        # R-13 (c): it did, and the record lost its close_reason.
        store = await self._store()
        await store.upsert(_record("closed", live=False).model_copy(update={"close_reason": "operator"}))

        await store.upsert(_record("interrupted", live=False))

        stored = await store.get("s1")
        self.assertEqual((stored.status, stored.close_reason), ("closed", "operator"))

    async def test_w4_10_a_record_that_cannot_be_read_fails_an_active_write_instead_of_being_replaced(self) -> None:
        # An I/O error read as "not closed", so a late active write replaced a closed record the error hid.
        store = await self._store()
        await store.upsert(_record("closed", live=False).model_copy(update={"close_reason": "operator"}))
        store.file_store.get = AsyncMock(side_effect=OSError("I/O error"))

        with self.assertLogs("app.session_store", "WARNING") as logged:
            with self.assertRaises(OSError):
                await store.upsert(_record("active", live=True))

        self.assertIn("s1", "\n".join(logged.output))
        del store.file_store.get  # the instance attribute: the real get again
        stored = await store.get("s1")
        self.assertEqual((stored.status, stored.close_reason), ("closed", "operator"))

    async def test_w4_10_a_missing_record_is_still_not_closed(self) -> None:
        store = await self._store()

        await store.upsert(_record("active", live=True))

        self.assertEqual((await store.get("s1")).status, "active")

    async def test_the_store_still_marks_active_records_interrupted_and_closes_them(self) -> None:
        store = DurableSessionStore(file_root=Path(self.tmp.name) / "rec", redis_url=None, redis_prefix="t")
        await store.startup()
        await store.upsert(_record("active", live=True))
        await store.mark_all_active_interrupted()
        self.assertEqual((await store.get("s1")).status, "interrupted")

        await store.upsert(_record("closed", live=False))

        self.assertEqual((await store.get("s1")).status, "closed")


if __name__ == "__main__":
    unittest.main()
