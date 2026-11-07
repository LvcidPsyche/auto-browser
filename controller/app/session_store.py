from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .models import SessionRecord
from .utils import PRIVATE_FILE_MODE, atomic_write_text, record_path, run_to_completion

logger = logging.getLogger(__name__)

# How many record ids the durable store remembers writing (the most recent), so that with Redis primary a missing file
# copy of one is known to have been deleted, not never written, and the closed guard asks Redis about it (R5C-16).
WRITTEN_IDS_REMEMBERED = 10_000

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
        path = record_path(self.root, session_id, ".json")
        if not path.exists():
            raise KeyError(session_id)
        return SessionRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def _upsert_sync(self, record: SessionRecord) -> None:
        path = record_path(self.root, record.id, ".json")
        atomic_write_text(path, record.model_dump_json(indent=2), mode=PRIVATE_FILE_MODE)


class RedisSessionStore(_MarkInterruptedMixin):
    def __init__(self, url: str, prefix: str):
        if redis_from_url is None:
            raise RuntimeError("redis package is not available")
        self.url = url
        self.prefix = prefix.rstrip(":")
        self.client: Redis | None = None

    async def startup(self) -> None:
        # Without timeouts a *hung* redis (as opposed to a refused connection)
        # blocks indefinitely. upsert() runs inside create_session and after
        # every successful action, so a silently wedged redis froze all session
        # activity with no error and no timeout to surface it.
        self.client = redis_from_url(
            self.url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
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

    async def delete(self, session_id: str) -> None:
        """Drop a record's copy here, so reads fall back to the file copy (list() skips an indexed id with none)."""
        await self._require_client().delete(self._record_key(session_id))

    def _require_client(self) -> Redis:
        if self.client is None:
            raise RuntimeError("redis session store is not started")
        return self.client

    def _record_key(self, session_id: str) -> str:
        return f"{self.prefix}:record:{session_id}"

    def _index_key(self) -> str:
        return f"{self.prefix}:index"


@dataclass
class _RecordLock:
    """One record id's write lock, and how many writes hold it or wait for it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


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
        # Serializes upsert() per record id within this process (CB3-2: one lock for the store queued every
        # session's persist behind one slow write); another controller process on the same store is not covered.
        # An entry goes once no write holds or waits for it, so the table does not grow with every session.
        self._write_locks: dict[str, _RecordLock] = {}
        # The ids this process wrote, oldest first, up to WRITTEN_IDS_REMEMBERED (_is_closed).
        self._written: OrderedDict[str, None] = OrderedDict()

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
        if self._primary is self.file_store:
            return await self.file_store.list()
        # Same reasoning as get(): the file records are the durable copy, so an
        # empty or unreachable Redis must not hide closed sessions or turn the
        # listing into an error. Redis records win for an id both stores hold,
        # except over a closed file copy (get()).
        file_records = {record.id: record for record in await self.file_store.list()}
        records = dict(file_records)
        try:
            redis_records = await self._primary.list()
        except Exception as exc:
            logger.warning("redis session listing failed, using file records: %s", exc)
            redis_records = []
        for record in redis_records:
            kept = file_records.get(record.id)
            if kept is not None and kept.status == "closed":
                continue
            records[record.id] = record
        return sorted(records.values(), key=lambda item: (item.created_at, item.id), reverse=True)

    async def get(self, session_id: str) -> SessionRecord:
        if self._primary is self.file_store:
            return await self.file_store.get(session_id)
        # upsert() writes the file record first, so it is the durable copy. A
        # Redis restart without persistence, an evicted key or a dropped
        # connection must not make a closed session look unowned to another
        # operator's ownership check.
        try:
            record = await self._primary.get(session_id)
        except KeyError:
            return await self.file_store.get(session_id)
        except Exception as exc:
            logger.warning("redis session read failed for %s, using file record: %s", session_id, exc)
            return await self.file_store.get(session_id)
        # The file copy is written first, so a closed one is the session's newest record. A write whose Redis half failed,
        # with Redis down for the drop of its copy too, left an older copy in Redis (R5C-5): the record from before the
        # close (a session never reopens), or a closed record from before a later save of it (the hosted Witness
        # delivery state that landed after the close).
        return await self._closed_file_copy(session_id) or record

    async def _closed_file_copy(self, session_id: str) -> SessionRecord | None:
        """The file copy when it says closed; None when it does not, or there is no readable one."""
        try:
            record = await self.file_store.get(session_id)
        except (KeyError, ValueError):
            return None
        except OSError as exc:
            logger.warning("could not read the file copy of session %s to check for a close: %s", session_id, exc)
            return None
        return record if record.status == "closed" else None

    async def upsert(self, record: SessionRecord) -> None:
        # One write at a time per record id, from the closed check through both backends, so a check cannot go stale
        # before its write lands, and the Redis copy lands in the same order as the file. Run to completion when the
        # caller is cancelled: a to_thread write keeps going after a cancel, so letting the lock go early reopens the
        # race.
        await run_to_completion(self._upsert_locked(record))

    @asynccontextmanager
    async def _record_lock(self, record_id: str) -> AsyncIterator[None]:
        entry = self._write_locks.get(record_id)
        if entry is None:
            entry = self._write_locks[record_id] = _RecordLock()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._write_locks.get(record_id) is entry:
                del self._write_locks[record_id]

    async def _upsert_locked(self, record: SessionRecord) -> None:
        async with self._record_lock(record.id):
            if record.status != "closed":
                closed = await self._is_closed(record.id)
                if closed is None:
                    # The stored record could not be read and could be a closed one: skip this write and go on (the
                    # record is a mirror), so the next write tries again.
                    return
                if closed:
                    # A late write of a session that has been closed: a session never reopens, and an interrupted
                    # write must not replace the close (and its close_reason) either.
                    logger.info("not writing a %s record over the closed session %s", record.status, record.id)
                    await self._repair_redis_copy(record.id)
                    return
            await self.file_store.upsert(record)
            self._written[record.id] = None
            self._written.move_to_end(record.id)
            if len(self._written) > WRITTEN_IDS_REMEMBERED:
                self._written.popitem(last=False)
            if self.redis_store is not None and self._primary is self.redis_store:
                try:
                    await self.redis_store.upsert(record)
                except Exception:
                    if record.status == "closed":
                        await self._drop_redis_copy(record.id)
                    raise

    async def _repair_redis_copy(self, session_id: str) -> None:
        """Give Redis the closed file record when the Redis copy is not a readable closed one (R5C-5).

        A close whose file write landed and whose Redis write failed left the old record in Redis, which get() and
        list() prefer, and a late write the closed guard skips is the only write that comes after it. Never raises.
        """
        if self.redis_store is None or self._primary is not self.redis_store:
            return
        try:
            try:
                if (await self.redis_store.get(session_id)).status == "closed":
                    return
            except (KeyError, ValueError):
                pass  # no copy, or one that cannot be parsed: the closed record replaces it
            await self.redis_store.upsert(await self.file_store.get(session_id))
        except Exception as exc:
            logger.warning("could not give redis the closed record of session %s: %s", session_id, exc)
            return
        logger.info("gave redis the closed record of session %s, which it held as not closed", session_id)

    async def _drop_redis_copy(self, session_id: str) -> None:
        """After a close reached the file copy but not Redis, drop the Redis copy, so reads fall back to the file
        instead of the record from before the close (R5C-5). Never raises: the write's own failure is raised."""
        try:
            await self.redis_store.delete(session_id)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("could not drop the redis copy of the closed session %s: %s", session_id, exc)

    async def _is_closed(self, session_id: str) -> bool | None:
        """Whether the durable (file) record says the session is closed, or None when that cannot be told.

        Never raises: the write it guards mirrors a change that has already taken effect. No record is not closed, and
        neither is one that cannot be parsed (no readable closed record), so the write replaces it. Any other read
        failure is None, so a non-closed write is skipped: an I/O error could be hiding a closed record.

        With Redis as the primary store, a file record that cannot be parsed defers to the Redis copy (W8-1), which
        get() reads first: a readable closed one refuses the write, a missing, unparseable or not closed one lets it
        replace both, and any other Redis read failure, such as a lost connection, is None. So does a missing file
        copy of a record this process wrote (R5C-16): it was deleted out of band, and Redis may hold its close. A
        record it never wrote (a new session's first write) asks nothing of Redis, which may be down.
        """
        redis_primary = self.redis_store is not None and self._primary is self.redis_store
        try:
            return (await self.file_store.get(session_id)).status == "closed"
        except KeyError:
            if redis_primary and session_id in self._written:
                return await self._redis_is_closed(session_id, "is missing")
            return False
        except ValueError as exc:
            if redis_primary:
                return await self._redis_is_closed(session_id, f"cannot be parsed ({exc})")
            logger.warning(
                "the stored record of session %s cannot be parsed, so this write replaces it: %s", session_id, exc
            )
            return False
        except Exception as exc:
            self._warn_unreadable(session_id, exc)
            return None

    async def _redis_is_closed(self, session_id: str, file_problem: str) -> bool | None:
        try:
            closed = (await self.redis_store.get(session_id)).status == "closed"  # type: ignore[union-attr]
        except (KeyError, ValueError):
            closed = False
        except Exception as exc:
            self._warn_unreadable(session_id, exc)
            return None
        if not closed:
            logger.warning(
                "the stored record of session %s %s and redis holds no closed one, so this write replaces it",
                session_id,
                file_problem,
            )
        return closed

    @staticmethod
    def _warn_unreadable(session_id: str, exc: BaseException) -> None:
        logger.warning(
            "could not read the record of session %s before writing it, so it is not written this time: %s",
            session_id,
            exc,
        )

    async def mark_all_active_interrupted(self) -> None:
        await self.file_store.mark_all_active_interrupted()
        if self.redis_store is None or self._primary is not self.redis_store:
            return
        for record in await self.redis_store.list():
            if record.status != "active":
                continue
            # A close that reached only the file copy (get()) gives Redis the closed record, not an interrupted one.
            closed = await self._closed_file_copy(record.id)
            await self.redis_store.upsert(closed or record.model_copy(update={"status": "interrupted", "live": False}))
