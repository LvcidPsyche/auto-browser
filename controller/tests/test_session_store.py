from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.models import SessionRecord
from app.session_store import DurableSessionStore, FileSessionStore, RedisSessionStore


def _record(session_id: str, *, status: str = "active", live: bool = True) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        name=f"session-{session_id}",
        created_at=f"2026-01-01T00:00:0{session_id[-1]}Z",
        updated_at="2026-01-01T00:00:00Z",
        status=status,  # type: ignore[arg-type]
        live=live,
        current_url="https://example.com",
        title="Example",
        artifact_dir="/tmp/artifacts",
        takeover_url="http://localhost:6080",
        remote_access={"active": True},
        isolation={"mode": "shared_browser_node"},
        auth_state={"available": False},
    )


class SessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_file_store_lists_gets_upserts_and_marks_active_records_interrupted(self) -> None:
        store = FileSessionStore(self.root)
        await store.startup()
        (self.root / "broken.json").write_text("{not json", encoding="utf-8")

        await store.upsert(_record("session-1", status="active", live=True))
        await store.upsert(_record("session-2", status="closed", live=False))

        records = await store.list()
        self.assertEqual([record.id for record in records], ["session-2", "session-1"])
        self.assertEqual((await store.get("session-1")).status, "active")

        await store.mark_all_active_interrupted()
        interrupted = await store.get("session-1")
        closed = await store.get("session-2")
        self.assertEqual(interrupted.status, "interrupted")
        self.assertFalse(interrupted.live)
        self.assertEqual(closed.status, "closed")
        with self.assertRaises(KeyError):
            await store.get("missing")
        await store.shutdown()

    async def test_durable_store_uses_file_backend_without_redis(self) -> None:
        store = DurableSessionStore(file_root=self.root, redis_url=None, redis_prefix="auto-browser:test")
        await store.startup()
        await store.upsert(_record("session-3"))

        self.assertEqual((await store.get("session-3")).id, "session-3")
        self.assertEqual([record.id for record in await store.list()], ["session-3"])

        await store.mark_all_active_interrupted()
        self.assertEqual((await store.get("session-3")).status, "interrupted")
        await store.shutdown()

    async def test_redis_store_uses_indexed_records_and_transactional_upsert(self) -> None:
        record = _record("session-4")
        missing_record = _record("session-5", status="closed", live=False)

        class FakePipeline:
            def __init__(self) -> None:
                self.set = AsyncMock()
                self.sadd = AsyncMock()
                self.execute = AsyncMock()

            async def __aenter__(self) -> "FakePipeline":
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

        class FakeRedis:
            def __init__(self) -> None:
                self.ping = AsyncMock()
                self.aclose = AsyncMock()
                self.smembers = AsyncMock(return_value={"session-4", "session-5"})
                self.mget = AsyncMock(return_value=[record.model_dump_json(), "{not json"])
                self.get = AsyncMock(side_effect=[record.model_dump_json(), None])
                self.pipeline_instance = FakePipeline()

            def pipeline(self, transaction: bool = True) -> FakePipeline:
                self.transaction = transaction
                return self.pipeline_instance

        client = FakeRedis()
        with patch("app.session_store.redis_from_url", Mock(return_value=client)):
            store = RedisSessionStore("redis://localhost:6379/0", "auto-browser:test:")
            await store.startup()
            await store.upsert(record)
            records = await store.list()
            fetched = await store.get("session-4")
            with self.assertRaises(KeyError):
                await store.get("missing")
            await store.shutdown()

        self.assertEqual([item.id for item in records], ["session-4"])
        self.assertEqual(fetched.id, "session-4")
        client.ping.assert_awaited_once()
        client.pipeline_instance.set.assert_awaited_once()
        client.pipeline_instance.sadd.assert_awaited_once()
        client.pipeline_instance.execute.assert_awaited_once()
        client.aclose.assert_awaited_once()
        self.assertEqual(missing_record.status, "closed")


if __name__ == "__main__":
    unittest.main()
