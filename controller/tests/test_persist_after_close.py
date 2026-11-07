"""A background persist never moves a closed session back to active (STORE-44)."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import session_store
from app.models import SessionRecord, WitnessRemoteState
from app.session_ownership import acting_as
from app.session_store import DurableSessionStore, RedisSessionStore
from app.utils import record_path
from tests.test_auth_state_ownership import ALICE, build_manager


class _MemoryPipeline:
    def __init__(self, client: _MemoryRedis) -> None:
        self.client = client
        self.writes: list[tuple[str, str]] = []
        self.members: list[str] = []

    async def __aenter__(self) -> _MemoryPipeline:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def set(self, key: str, value: str) -> None:
        self.writes.append((key, value))

    async def sadd(self, _key: str, *members: str) -> None:
        self.members.extend(members)

    async def execute(self) -> None:
        failure = self.client.fail or self.client.write_fail
        if failure is not None:
            raise failure
        self.client.values.update(self.writes)
        self.client.index.update(self.members)


class _MemoryRedis:
    """An in-memory stand-in for the redis client RedisSessionStore uses: get, mget, smembers (its one index), delete,
    and a pipeline's set, sadd and execute. `fail` fails every call; `write_fail` only the pipeline's writes."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.index: set[str] = set()
        self.fail: Exception | None = None
        self.write_fail: Exception | None = None

    async def get(self, key: str) -> str | None:
        if self.fail is not None:
            raise self.fail
        return self.values.get(key)

    async def mget(self, keys: list[str]) -> list[str | None]:
        if self.fail is not None:
            raise self.fail
        return [self.values.get(key) for key in keys]

    async def smembers(self, _key: str) -> set[str]:
        if self.fail is not None:
            raise self.fail
        return set(self.index)

    async def delete(self, *keys: str) -> int:
        if self.fail is not None:
            raise self.fail
        return sum(self.values.pop(key, None) is not None for key in keys)

    def pipeline(self, transaction: bool = True) -> _MemoryPipeline:
        return _MemoryPipeline(self)


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

    async def test_w8_2_a_close_writes_its_record_over_one_it_cannot_parse(self) -> None:
        # The pre-read caught only KeyError: pydantic's ValidationError reached the outer handler, which logged an
        # error and wrote no closed record.
        record_path(self.manager.session_store.file_store.root, self.session_id, ".json").write_text(
            "{not json", encoding="utf-8"
        )
        self.manager._session_summary = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("summary failed")
        )

        with acting_as(ALICE), self.assertRaises(RuntimeError):
            await self.manager.close_session(self.session_id, reason="operator")

        stored = await self.manager.session_store.get(self.session_id)
        self.assertEqual((stored.status, stored.close_reason, stored.live), ("closed", "operator", False))

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

    async def test_w6_1_a_record_that_cannot_be_read_skips_an_active_write_without_raising(self) -> None:
        # W4-10: an I/O error read as "not closed", so a late active write replaced a closed record the error hid.
        # W6-1: raising instead failed every later write, after the action it mirrors had taken effect; skip it.
        store = await self._store()
        await store.upsert(_record("closed", live=False).model_copy(update={"close_reason": "operator"}))
        store.file_store.get = AsyncMock(side_effect=OSError("I/O error"))

        with self.assertLogs("app.session_store", "WARNING") as logged:
            await store.upsert(_record("active", live=True))

        self.assertIn("s1", "\n".join(logged.output))
        del store.file_store.get  # the instance attribute: the real get again
        stored = await store.get("s1")
        self.assertEqual((stored.status, stored.close_reason), ("closed", "operator"))

    async def test_w6_1_a_closed_write_lands_when_the_record_cannot_be_read(self) -> None:
        store = await self._store()
        await store.upsert(_record("active", live=True))
        store.file_store.get = AsyncMock(side_effect=OSError("I/O error"))

        await store.upsert(_record("closed", live=False))

        del store.file_store.get
        self.assertEqual((await store.get("s1")).status, "closed")

    async def test_w6_1_a_corrupt_record_is_replaced_by_the_next_active_write(self) -> None:
        store = await self._store()
        await store.upsert(_record("active", live=True))
        record_path(Path(self.tmp.name) / "rec", "s1", ".json").write_text("{not json", encoding="utf-8")

        with self.assertLogs("app.session_store", "WARNING") as logged:
            await store.upsert(_record("active", live=True))

        self.assertIn("s1", "\n".join(logged.output))
        self.assertEqual((await store.get("s1")).status, "active")

    async def test_w6_1_a_record_that_fails_validation_is_replaced_by_the_next_active_write(self) -> None:
        store = await self._store()
        await store.upsert(_record("active", live=True))
        record_path(Path(self.tmp.name) / "rec", "s1", ".json").write_text('{"id": "s1"}', encoding="utf-8")

        await store.upsert(_record("active", live=True))

        self.assertEqual((await store.get("s1")).status, "active")

    async def _store_with_redis(self) -> tuple[DurableSessionStore, _MemoryRedis]:
        # The real RedisSessionStore over an in-memory client, as startup() leaves it when Redis answers.
        store = await self._store()
        client = _MemoryRedis()
        redis = RedisSessionStore("redis://127.0.0.1:6379/0", "t")
        redis.client = client  # type: ignore[assignment]
        store.redis_store = redis
        store._primary = redis
        return store, client

    async def test_w8_1_with_redis_a_corrupt_file_copy_of_a_closed_record_refuses_an_active_write(self) -> None:
        # W6-1 read only the file copy: one that cannot be parsed counted as not closed, so a late active write
        # replaced the readable closed record in Redis too.
        store, client = await self._store_with_redis()
        await store.upsert(_record("closed", live=False).model_copy(update={"close_reason": "operator"}))
        path = record_path(Path(self.tmp.name) / "rec", "s1", ".json")
        path.write_text("{not json", encoding="utf-8")

        await store.upsert(_record("active", live=True))

        stored = SessionRecord.model_validate_json(client.values["t:record:s1"])
        self.assertEqual((stored.status, stored.close_reason), ("closed", "operator"))
        self.assertEqual((await store.get("s1")).status, "closed")
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    async def test_w8_1_with_redis_a_corrupt_file_copy_is_replaced_when_redis_holds_no_closed_record(self) -> None:
        for redis_value in (None, "{not json", _record("active", live=True).model_dump_json()):
            with self.subTest(redis_value=redis_value):
                store, client = await self._store_with_redis()
                await store.upsert(_record("active", live=True))
                record_path(Path(self.tmp.name) / "rec", "s1", ".json").write_text("{not json", encoding="utf-8")
                if redis_value is None:
                    client.values.clear()
                else:
                    client.values["t:record:s1"] = redis_value

                await store.upsert(_record("active", live=True).model_copy(update={"title": "replaced"}))

                self.assertEqual((await store.file_store.get("s1")).title, "replaced")
                self.assertEqual(SessionRecord.model_validate_json(client.values["t:record:s1"]).title, "replaced")

    async def test_w8_1_with_redis_a_corrupt_file_copy_and_an_unreadable_redis_skip_an_active_write(self) -> None:
        store, client = await self._store_with_redis()
        await store.upsert(_record("closed", live=False))
        path = record_path(Path(self.tmp.name) / "rec", "s1", ".json")
        path.write_text("{not json", encoding="utf-8")
        client.fail = ConnectionError("redis down")

        with self.assertLogs("app.session_store", "WARNING") as logged:
            await store.upsert(_record("active", live=True))

        self.assertIn("so it is not written this time", "\n".join(logged.output))
        client.fail = None
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")
        self.assertEqual(SessionRecord.model_validate_json(client.values["t:record:s1"]).status, "closed")

    async def test_r5c_5_a_skipped_late_write_repairs_a_redis_copy_left_active_by_a_failed_close(self) -> None:
        # A close whose file write landed and whose Redis write failed left the active record in Redis, which get()
        # and list() prefer; the late write the closed guard skipped was the only write left, and it repaired nothing.
        store, client = await self._store_with_redis()
        await store.upsert(_record("active", live=True))
        await store.file_store.upsert(_record("closed", live=False).model_copy(update={"close_reason": "operator"}))
        self.assertEqual(SessionRecord.model_validate_json(client.values["t:record:s1"]).status, "active")

        await store.upsert(_record("active", live=True))

        stored = SessionRecord.model_validate_json(client.values["t:record:s1"])
        self.assertEqual((stored.status, stored.close_reason), ("closed", "operator"))
        self.assertEqual((await store.get("s1")).status, "closed")

    async def test_r5c_5_a_close_whose_redis_write_fails_drops_the_stale_redis_copy(self) -> None:
        store, client = await self._store_with_redis()
        await store.upsert(_record("active", live=True))
        client.write_fail = ConnectionError("redis write lost")

        with self.assertRaises(ConnectionError):
            await store.upsert(_record("closed", live=False))

        client.write_fail = None
        self.assertNotIn("t:record:s1", client.values)  # so reads fall back to the durable file copy
        self.assertEqual((await store.get("s1")).status, "closed")

    async def test_r5c_5_a_dropped_redis_copy_leaves_the_session_listed_from_its_file_copy(self) -> None:
        # GLM review, not reproduced: the drop leaves the id in Redis's index, which Redis's own listing skips, and
        # the store's listing still holds the session, from the file copy it merges first.
        store, client = await self._store_with_redis()
        await store.upsert(_record("active", live=True))
        client.write_fail = ConnectionError("redis write lost")
        with self.assertRaises(ConnectionError):
            await store.upsert(_record("closed", live=False))
        client.write_fail = None

        self.assertIn("s1", client.index)
        self.assertEqual([(record.id, record.status) for record in await store.list()], [("s1", "closed")])

    async def _close_lost_by_an_unreachable_redis(self) -> tuple[DurableSessionStore, _MemoryRedis]:
        store, client = await self._store_with_redis()
        await store.upsert(_record("active", live=True))
        client.fail = ConnectionError("redis down")  # the close's write and the drop of the old copy both fail
        with self.assertRaises(ConnectionError):
            await store.upsert(_record("closed", live=False).model_copy(update={"close_reason": "operator"}))
        client.fail = None
        self.assertEqual(SessionRecord.model_validate_json(client.values["t:record:s1"]).status, "active")
        return store, client

    async def test_r5c_5_a_close_redis_never_got_reads_closed_once_redis_is_back(self) -> None:
        # Codex review: with Redis down for the close's write and for the drop, it kept the active record, which reads
        # and listings preferred, and only a later write the closed guard skipped repaired it. A session never
        # reopens: a closed file copy now wins over a Redis copy that is not closed.
        store, _ = await self._close_lost_by_an_unreachable_redis()

        self.assertEqual((await store.get("s1")).close_reason, "operator")
        self.assertEqual([(record.id, record.status) for record in await store.list()], [("s1", "closed")])

    async def test_r5c_5_a_restart_gives_redis_the_close_it_never_got_instead_of_interrupting_it(self) -> None:
        store, client = await self._close_lost_by_an_unreachable_redis()
        await store.upsert(_record("active", live=True).model_copy(update={"id": "s2"}))

        await store.mark_all_active_interrupted()  # the controller's startup

        stored = SessionRecord.model_validate_json(client.values["t:record:s1"])
        self.assertEqual((stored.status, stored.close_reason), ("closed", "operator"))
        self.assertEqual(SessionRecord.model_validate_json(client.values["t:record:s2"]).status, "interrupted")
        self.assertEqual((await store.file_store.get("s2")).status, "interrupted")

    async def test_r5c_5_a_closed_records_later_save_redis_never_got_reads_from_the_file_copy(self) -> None:
        # Codex review of c1: a closed record saved again (a hosted Witness outcome that landed after the close) reached
        # the file copy while Redis, down for the write and for the drop of its copy, kept the closed record from
        # before; reads and listings took any closed Redis copy, so the later save never showed once Redis was back.
        store, client = await self._store_with_redis()
        closed = _record("closed", live=False)
        await store.upsert(closed.model_copy(update={"witness_remote": WitnessRemoteState(status="delivered")}))
        client.fail = ConnectionError("redis down")
        with self.assertRaises(ConnectionError):
            await store.upsert(closed.model_copy(update={"witness_remote": WitnessRemoteState(status="failed")}))
        client.fail = None
        stale = SessionRecord.model_validate_json(client.values["t:record:s1"])
        self.assertEqual((stale.status, stale.witness_remote.status), ("closed", "delivered"))

        self.assertEqual((await store.get("s1")).witness_remote.status, "failed")
        self.assertEqual([record.witness_remote.status for record in await store.list()], ["failed"])

    async def test_r5c_5_a_failed_active_write_keeps_the_redis_copy(self) -> None:
        store, client = await self._store_with_redis()
        await store.upsert(_record("active", live=True))
        client.write_fail = ConnectionError("redis write lost")

        with self.assertRaises(ConnectionError):
            await store.upsert(_record("active", live=True).model_copy(update={"title": "newer"}))

        self.assertEqual(SessionRecord.model_validate_json(client.values["t:record:s1"]).status, "active")

    async def test_r5c_16_a_deleted_file_copy_of_a_closed_record_refuses_a_late_active_write(self) -> None:
        # The closed guard read only the file copy, and a missing one meant "not closed": with the copy deleted out of
        # band, a late write replaced the closed record Redis still held.
        store, client = await self._store_with_redis()
        await store.upsert(_record("active", live=True))
        await store.upsert(_record("closed", live=False).model_copy(update={"close_reason": "operator"}))
        path = record_path(Path(self.tmp.name) / "rec", "s1", ".json")
        path.unlink()

        await store.upsert(_record("active", live=True))

        stored = SessionRecord.model_validate_json(client.values["t:record:s1"])
        self.assertEqual((stored.status, stored.close_reason), ("closed", "operator"))
        self.assertEqual((await store.get("s1")).status, "closed")
        self.assertFalse(path.exists())

    async def test_r5c_16_a_deleted_file_copy_and_an_unreadable_redis_skip_a_late_active_write(self) -> None:
        store, client = await self._store_with_redis()
        await store.upsert(_record("closed", live=False))
        path = record_path(Path(self.tmp.name) / "rec", "s1", ".json")
        path.unlink()
        client.fail = ConnectionError("redis down")

        with self.assertLogs("app.session_store", "WARNING") as logged:
            await store.upsert(_record("active", live=True))

        self.assertIn("so it is not written this time", "\n".join(logged.output))
        self.assertFalse(path.exists())

    async def test_r5c_16_a_deleted_file_copy_of_an_open_record_is_written_again(self) -> None:
        store, client = await self._store_with_redis()
        await store.upsert(_record("active", live=True))
        record_path(Path(self.tmp.name) / "rec", "s1", ".json").unlink()

        await store.upsert(_record("active", live=True).model_copy(update={"title": "again"}))

        self.assertEqual((await store.file_store.get("s1")).title, "again")
        self.assertEqual(SessionRecord.model_validate_json(client.values["t:record:s1"]).title, "again")

    async def test_r5c_16_a_new_sessions_first_write_asks_nothing_of_a_redis_that_is_down(self) -> None:
        store, client = await self._store_with_redis()
        client.fail = ConnectionError("redis down")

        with self.assertRaises(ConnectionError):  # the Redis write itself fails, as before
            await store.upsert(_record("active", live=True))

        self.assertEqual((await store.file_store.get("s1")).status, "active")  # no Redis read refused the file copy

    async def test_r5c_16_the_store_remembers_only_its_most_recent_writes(self) -> None:
        store, _ = await self._store_with_redis()

        with patch.object(session_store, "WRITTEN_IDS_REMEMBERED", 2):
            for session_id in ("a", "b", "a", "c"):
                await store.upsert(_record("active", live=True).model_copy(update={"id": session_id}))

        self.assertEqual(list(store._written), ["a", "c"])  # "a" written again is recent; "b" is the oldest, dropped

    async def test_w8_5_a_record_with_bad_utf8_is_replaced_by_the_next_active_write(self) -> None:
        # UnicodeDecodeError is a ValueError: the record cannot be parsed, so the write replaces it.
        store = await self._store()
        await store.upsert(_record("closed", live=False))
        record_path(Path(self.tmp.name) / "rec", "s1", ".json").write_bytes(b'{"id": "s1", "status": "\xff"}')

        with self.assertLogs("app.session_store", "WARNING") as logged:
            await store.upsert(_record("active", live=True))

        self.assertIn("cannot be parsed", "\n".join(logged.output))
        self.assertEqual((await store.get("s1")).status, "active")

    async def test_w8_5_a_directory_in_the_records_place_skips_an_active_write(self) -> None:
        # IsADirectoryError on Linux, PermissionError on Windows: both OSError, so the write is skipped, not raised.
        store = await self._store()
        path = record_path(Path(self.tmp.name) / "rec", "s1", ".json")
        path.mkdir()

        with self.assertLogs("app.session_store", "WARNING") as logged:
            await store.upsert(_record("active", live=True))

        self.assertIn("so it is not written this time", "\n".join(logged.output))
        self.assertTrue(path.is_dir())

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
