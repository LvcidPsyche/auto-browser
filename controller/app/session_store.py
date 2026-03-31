from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

from .models import SessionRecord

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import path depends on optional dependency
    from redis.asyncio import Redis
    from redis.asyncio import from_url as redis_from_url
except Exception:  # pragma: no cover - graceful fallback when redis isn't installed
    Redis = None  # type: ignore[assignment]
    redis_from_url = None


class SessionStoreBackend(Protocol):
    async def startup(self) -> None: ...
    async def shutdown(self) -> None: ...
    async def list(self) -> list[SessionRecord]: ...
    async def get(self, session_id: str) -> SessionRecord: ...
    async def upsert(self, record: SessionRecord) -> None: ...
    async def mark_all_active_interrupted(self) -> None: ...


class _MarkInterruptedMixin:
    """Shared mark_all_active_interrupted for stores that implement list() + upsert()."""

    async def list(self) -> list[SessionRecord]: ...  # type: ignore[empty-body]
    async def upsert(self, record: SessionRecord) -> None: ...  # type: ignore[empty-body]

    async def mark_all_active_interrupted(self) -> None:
        records = await self.list()
        for record in records:
            if record.status == "active":
                record.status = "interrupted"
                record.live = False
                await self.upsert(record)


class FileSessionStore(_MarkInterruptedMixin):
    def __init__(self, root: str | Path):
        self.root = Path(root)

    async def startup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    async def shutdown(self) -> None:
        return None

    async def list(self) -> list[SessionRecord]:
        return await asyncio.to_thread(self._list_sync)

    async def get(self, session_id: str) -> SessionRecord:
        return await asyncio.to_thread(self._get_sync, session_id)

    async def upsert(self, record: SessionRecord) -> None:
        await asyncio.to_thread(self._upsert_sync, record)

    def _list_sync(self) -> list[SessionRecord]:
        records: list[SessionRecord] = []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            try:
                records.append(SessionRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.warning("failed to read session record %s: %s", path, exc)
        records.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return records

    def _get_sync(self, session_id: str) -> SessionRecord:
        path = self.root / f"{session_id}.json"
        if not path.exists():
            raise KeyError(session_id)
        return SessionRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def _upsert_sync(self, record: SessionRecord) -> None:
        path = self.root / f"{record.id}.json"
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        tmp_path.replace(path)


class RedisSessionStore(_MarkInterruptedMixin):
    def __init__(self, url: str, prefix: str):
        if redis_from_url is None:
            raise RuntimeError("redis package is not available")
        self.url = url
        self.prefix = prefix.rstrip(":")
        self.client: Redis | None = None

    async def startup(self) -> None:
        self.client = redis_from_url(self.url, decode_responses=True)
        await self.client.ping()

    async def shutdown(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def list(self) -> list[SessionRecord]:
        client = self._require_client()
        ids = sorted(await client.smembers(self._index_key()))
        if not ids:
            return []
        payloads = await client.mget([self._record_key(session_id) for session_id in ids])
        records: list[SessionRecord] = []
        for payload in payloads:
            if not payload:
                continue
            try:
                records.append(SessionRecord.model_validate_json(payload))
            except Exception as exc:
                logger.warning("failed to decode redis session record: %s", exc)
        records.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return records

    async def get(self, session_id: str) -> SessionRecord:
        client = self._require_client()
        payload = await client.get(self._record_key(session_id))
        if not payload:
            raise KeyError(session_id)
        return SessionRecord.model_validate_json(payload)

    async def upsert(self, record: SessionRecord) -> None:
        client = self._require_client()
        async with client.pipeline(transaction=True) as pipe:
            await pipe.set(self._record_key(record.id), record.model_dump_json())
            await pipe.sadd(self._index_key(), record.id)
            await pipe.execute()

    def _require_client(self) -> Redis:
        if self.client is None:
            raise RuntimeError("redis session store is not started")
        return self.client

    def _record_key(self, session_id: str) -> str:
        return f"{self.prefix}:record:{session_id}"

    def _index_key(self) -> str:
        return f"{self.prefix}:index"


class DurableSessionStore:
    def __init__(
        self,
        *,
        file_root: str | Path,
        redis_url: str | None,
        redis_prefix: str,
    ):
        self.file_store = FileSessionStore(file_root)
        self.redis_store = RedisSessionStore(redis_url, redis_prefix) if redis_url else None
        self._primary: SessionStoreBackend = self.file_store

    async def startup(self) -> None:
        await self.file_store.startup()
        if self.redis_store is None:
            self._primary = self.file_store
            return
        try:
            await self.redis_store.startup()
        except Exception as exc:
            logger.warning("redis session store unavailable, using file store fallback: %s", exc)
            self._primary = self.file_store
            return
        self._primary = self.redis_store

    async def shutdown(self) -> None:
        await self.file_store.shutdown()
        if self.redis_store is not None:
            await self.redis_store.shutdown()

    async def list(self) -> list[SessionRecord]:
        return await self._primary.list()

    async def get(self, session_id: str) -> SessionRecord:
        return await self._primary.get(session_id)

    async def upsert(self, record: SessionRecord) -> None:
        await self.file_store.upsert(record)
        if self.redis_store is not None and self._primary is self.redis_store:
            await self.redis_store.upsert(record)

    async def mark_all_active_interrupted(self) -> None:
        await self.file_store.mark_all_active_interrupted()
        if self.redis_store is not None and self._primary is self.redis_store:
            await self.redis_store.mark_all_active_interrupted()
