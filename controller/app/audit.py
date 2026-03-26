from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .models import AuditEvent, OperatorIdentity
from .utils import utc_now

logger = logging.getLogger(__name__)

_CURRENT_OPERATOR: ContextVar[OperatorIdentity] = ContextVar(
    "current_operator",
    default=OperatorIdentity(id="anonymous", source="anonymous"),
)


def set_current_operator(operator_id: str | None, *, name: str | None = None, source: str = "header") -> Token:
    identity = OperatorIdentity(
        id=(operator_id or "anonymous").strip() or "anonymous",
        name=(name or None),
        source=source if operator_id else "anonymous",
    )
    return _CURRENT_OPERATOR.set(identity)


def reset_current_operator(token: Token) -> None:
    _CURRENT_OPERATOR.reset(token)


def get_current_operator() -> OperatorIdentity:
    return _CURRENT_OPERATOR.get()


class AuditStoreBackend(Protocol):
    async def startup(self) -> None: ...
    async def list(
        self,
        *,
        limit: int = 100,
        session_id: str | None = None,
        event_type: str | None = None,
        operator_id: str | None = None,
    ) -> list[AuditEvent]: ...
    async def append_event(self, event: AuditEvent) -> None: ...


class FileAuditStore:
    def __init__(self, root: str | Path, *, max_events: int, trim_interval: int = 500):
        self.root = Path(root)
        self.events_path = self.root / "events.jsonl"
        self.max_events = max(0, max_events)
        self.trim_interval = max(1, trim_interval)
        self._writes_since_trim = 0

    async def startup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    async def append_event(self, event: AuditEvent) -> None:
        line = event.model_dump_json()
        await asyncio.to_thread(self._append_text, self.events_path, line + "\n")
        self._writes_since_trim += 1
        if self.max_events and self._writes_since_trim >= self.trim_interval:
            await asyncio.to_thread(self._trim_sync)
            self._writes_since_trim = 0

    async def list(
        self,
        *,
        limit: int = 100,
        session_id: str | None = None,
        event_type: str | None = None,
        operator_id: str | None = None,
    ) -> list[AuditEvent]:
        return await asyncio.to_thread(
            self._list_sync,
            limit,
            session_id,
            event_type,
            operator_id,
        )

    def _list_sync(
        self,
        limit: int,
        session_id: str | None,
        event_type: str | None,
        operator_id: str | None,
    ) -> list[AuditEvent]:
        if not self.events_path.exists():
            return []
        events: list[AuditEvent] = []
        with self.events_path.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                event = AuditEvent.model_validate_json(raw)
                if session_id and event.session_id != session_id:
                    continue
                if event_type and event.event_type != event_type:
                    continue
                if operator_id and event.operator.id != operator_id:
                    continue
                events.append(event)
        events.sort(key=lambda item: (item.timestamp, item.id), reverse=True)
        return events[:limit]

    def _trim_sync(self) -> None:
        if not self.events_path.exists() or self.max_events <= 0:
            return
        lines = [line for line in self.events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(lines) <= self.max_events:
            return
        trimmed = "\n".join(lines[-self.max_events :]) + "\n"
        tmp_path = self.events_path.with_suffix(".jsonl.tmp")
        tmp_path.write_text(trimmed, encoding="utf-8")
        tmp_path.replace(self.events_path)

    @staticmethod
    def _append_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)


class SQLiteAuditStore:
    def __init__(self, db_path: str | Path, *, max_events: int):
        self.db_path = Path(db_path)
        self.max_events = max(0, max_events)

    async def startup(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._startup_sync)

    async def append_event(self, event: AuditEvent) -> None:
        await asyncio.to_thread(self._append_sync, event)

    async def list(
        self,
        *,
        limit: int = 100,
        session_id: str | None = None,
        event_type: str | None = None,
        operator_id: str | None = None,
    ) -> list[AuditEvent]:
        return await asyncio.to_thread(self._list_sync, limit, session_id, event_type, operator_id)

    def _startup_sync(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    action TEXT,
                    session_id TEXT,
                    approval_id TEXT,
                    job_id TEXT,
                    operator_id TEXT,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_events_timestamp ON audit_events(timestamp DESC, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_events_session ON audit_events(session_id, timestamp DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_events_type ON audit_events(event_type, timestamp DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_events_operator ON audit_events(operator_id, timestamp DESC)"
            )
            conn.commit()

    def _append_sync(self, event: AuditEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO audit_events (
                    id, timestamp, event_type, status, action, session_id, approval_id, job_id, operator_id, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.timestamp,
                    event.event_type,
                    event.status,
                    event.action,
                    event.session_id,
                    event.approval_id,
                    event.job_id,
                    event.operator.id,
                    event.model_dump_json(),
                ),
            )
            if self.max_events > 0:
                conn.execute(
                    """
                    DELETE FROM audit_events
                    WHERE id IN (
                        SELECT id FROM audit_events
                        ORDER BY timestamp DESC, id DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (self.max_events,),
                )
            conn.commit()

    def _list_sync(
        self,
        limit: int,
        session_id: str | None,
        event_type: str | None,
        operator_id: str | None,
    ) -> list[AuditEvent]:
        query = "SELECT payload FROM audit_events WHERE 1=1"
        params: list[str] = []
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        if operator_id:
            query += " AND operator_id = ?"
            params.append(operator_id)
        query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(str(limit))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [AuditEvent.model_validate_json(row[0]) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn


class AuditStore:
    def __init__(
        self,
        root: str | Path,
        *,
        db_path: str | None = None,
        max_events: int = 10000,
        file_trim_interval: int = 500,
    ):
        self._lock = asyncio.Lock()
        self.file_store = FileAuditStore(root, max_events=max_events, trim_interval=file_trim_interval)
        self.sqlite_store = SQLiteAuditStore(db_path, max_events=max_events) if db_path else None
        self._primary: AuditStoreBackend = self.file_store

    async def startup(self) -> None:
        await self.file_store.startup()
        if self.sqlite_store is None:
            self._primary = self.file_store
            return
        try:
            await self.sqlite_store.startup()
        except Exception as exc:
            logger.warning("sqlite audit store unavailable, using file store fallback: %s", exc)
            self._primary = self.file_store
            return
        self._primary = self.sqlite_store

    async def append(
        self,
        *,
        event_type: str,
        status: str,
        action: str | None = None,
        session_id: str | None = None,
        approval_id: str | None = None,
        job_id: str | None = None,
        operator: OperatorIdentity | None = None,
        details: dict | None = None,
    ) -> AuditEvent:
        async with self._lock:
            event = AuditEvent(
                id=uuid4().hex[:12],
                timestamp=utc_now(),
                event_type=event_type,
                status=status,
                action=action,
                session_id=session_id,
                approval_id=approval_id,
                job_id=job_id,
                operator=operator or get_current_operator(),
                details=details or {},
            )
            await self.file_store.append_event(event)
            if self.sqlite_store is not None and self._primary is self.sqlite_store:
                await self.sqlite_store.append_event(event)
            return event

    async def list(
        self,
        *,
        limit: int = 100,
        session_id: str | None = None,
        event_type: str | None = None,
        operator_id: str | None = None,
    ) -> list[AuditEvent]:
        return await self._primary.list(
            limit=limit,
            session_id=session_id,
            event_type=event_type,
            operator_id=operator_id,
        )

