from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .models import ApprovalKind, ApprovalRecord, ApprovalStatus, BrowserActionDecision
from .utils import UTC, utc_now

logger = logging.getLogger(__name__)


class ApprovalRequiredError(RuntimeError):
    def __init__(self, approval: ApprovalRecord, message: str | None = None):
        self.approval = approval
        self.payload = {
            "status": "approval_required",
            "message": message or f"{approval.kind} actions require human approval",
            "approval": approval.model_dump(),
        }
        super().__init__(self.payload["message"])


class ApprovalStoreBackend(Protocol):
    async def startup(self) -> None: ...
    async def list(
        self,
        *,
        status: ApprovalStatus | None = None,
        session_id: str | None = None,
    ) -> list[ApprovalRecord]: ...
    async def get(self, approval_id: str) -> ApprovalRecord: ...
    async def upsert(self, approval: ApprovalRecord) -> None: ...


class FileApprovalStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    async def startup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    async def list(
        self,
        *,
        status: ApprovalStatus | None = None,
        session_id: str | None = None,
    ) -> list[ApprovalRecord]:
        return await asyncio.to_thread(self._list_sync, status, session_id)

    async def get(self, approval_id: str) -> ApprovalRecord:
        return await asyncio.to_thread(self._read_sync, approval_id)

    async def upsert(self, approval: ApprovalRecord) -> None:
        await asyncio.to_thread(self._write_sync, approval)

    def _list_sync(
        self,
        status: ApprovalStatus | None,
        session_id: str | None,
    ) -> list[ApprovalRecord]:
        approvals: list[ApprovalRecord] = []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            try:
                approval = ApprovalRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.debug("skipping unreadable approval file %s: %s", path.name, exc)
                continue
            if status is not None and approval.status != status:
                continue
            if session_id is not None and approval.session_id != session_id:
                continue
            approvals.append(approval)
        approvals.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return approvals

    def _read_sync(self, approval_id: str) -> ApprovalRecord:
        path = self._path(approval_id)
        if not path.exists():
            raise KeyError(approval_id)
        return ApprovalRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def _write_sync(self, approval: ApprovalRecord) -> None:
        path = self._path(approval.id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(approval.model_dump_json(indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _path(self, approval_id: str) -> Path:
        return self.root / f"{approval_id}.json"


class SQLiteApprovalStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    async def startup(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._startup_sync)

    async def list(
        self,
        *,
        status: ApprovalStatus | None = None,
        session_id: str | None = None,
    ) -> list[ApprovalRecord]:
        return await asyncio.to_thread(self._list_sync, status, session_id)

    async def get(self, approval_id: str) -> ApprovalRecord:
        return await asyncio.to_thread(self._get_sync, approval_id)

    async def upsert(self, approval: ApprovalRecord) -> None:
        await asyncio.to_thread(self._upsert_sync, approval)

    def _startup_sync(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_session ON approvals(session_id, created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at DESC)")
            conn.commit()

    def _list_sync(
        self,
        status: ApprovalStatus | None,
        session_id: str | None,
    ) -> list[ApprovalRecord]:
        query = "SELECT payload FROM approvals WHERE 1=1"
        params: list[str] = []
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        query += " ORDER BY created_at DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [ApprovalRecord.model_validate_json(row[0]) for row in rows]

    def _get_sync(self, approval_id: str) -> ApprovalRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(approval_id)
        return ApprovalRecord.model_validate_json(row[0])

    def _upsert_sync(self, approval: ApprovalRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO approvals (
                    id, session_id, kind, status, created_at, updated_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.id,
                    approval.session_id,
                    approval.kind,
                    approval.status,
                    approval.created_at,
                    approval.updated_at,
                    approval.model_dump_json(),
                ),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn


class ApprovalStore:
    def __init__(
        self,
        root: str | Path,
        db_path: str | None = None,
        *,
        approval_ttl_minutes: int = 15,
    ):
        self._lock = asyncio.Lock()
        self.file_store = FileApprovalStore(root)
        self.sqlite_store = SQLiteApprovalStore(db_path) if db_path else None
        self._primary: ApprovalStoreBackend = self.file_store
        self.approval_ttl = timedelta(minutes=max(1, approval_ttl_minutes))

    async def startup(self) -> None:
        await self.file_store.startup()
        if self.sqlite_store is None:
            self._primary = self.file_store
            return
        try:
            await self.sqlite_store.startup()
        except Exception as exc:
            logger.warning("sqlite approval store unavailable, using file store fallback: %s", exc)
            self._primary = self.file_store
            return
        self._primary = self.sqlite_store

    async def list(
        self,
        *,
        status: ApprovalStatus | None = None,
        session_id: str | None = None,
    ) -> list[ApprovalRecord]:
        return await self._primary.list(status=status, session_id=session_id)

    async def get(self, approval_id: str) -> ApprovalRecord:
        return await self._primary.get(approval_id)

    async def create_or_reuse_pending(
        self,
        *,
        session_id: str,
        kind: ApprovalKind,
        reason: str,
        action: BrowserActionDecision,
        observation: dict | None = None,
    ) -> ApprovalRecord:
        async with self._lock:
            existing = await self._find_matching_pending(session_id=session_id, kind=kind, action=action)
            if existing is not None:
                return existing

            now = utc_now()
            approval = ApprovalRecord(
                id=uuid4().hex[:12],
                session_id=session_id,
                kind=kind,
                status="pending",
                created_at=now,
                updated_at=now,
                reason=reason,
                action=action,
                observation=observation,
            )
            await self._persist(approval)
            return approval

    async def approve(self, approval_id: str, comment: str | None = None) -> ApprovalRecord:
        return await self._transition(approval_id, status="approved", comment=comment)

    async def reject(self, approval_id: str, comment: str | None = None) -> ApprovalRecord:
        return await self._transition(approval_id, status="rejected", comment=comment)

    async def mark_executed(self, approval_id: str) -> ApprovalRecord:
        async with self._lock:
            approval = await self.get(approval_id)
            if approval.status != "approved":
                raise PermissionError(f"approval {approval_id} is not approved")
            self._ensure_not_expired(approval)
            now = utc_now()
            approval.status = "executed"
            approval.updated_at = now
            approval.executed_at = now
            await self._persist(approval)
            return approval

    async def require_approved(
        self,
        *,
        approval_id: str,
        session_id: str,
        kind: ApprovalKind,
        action: BrowserActionDecision,
    ) -> ApprovalRecord:
        approval = await self.get(approval_id)
        if approval.session_id != session_id:
            raise PermissionError(f"approval {approval_id} does not belong to session {session_id}")
        if approval.kind != kind:
            raise PermissionError(f"approval {approval_id} does not cover {kind}")
        if approval.status != "approved":
            raise PermissionError(f"approval {approval_id} is not approved")
        self._ensure_not_expired(approval)
        if not self._actions_match(approval.action, action):
            raise PermissionError(f"approval {approval_id} does not match the requested action")
        return approval

    async def _transition(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        comment: str | None,
    ) -> ApprovalRecord:
        async with self._lock:
            approval = await self.get(approval_id)
            if approval.status == "executed":
                raise PermissionError(f"approval {approval_id} has already been executed")
            now = utc_now()
            approval.status = status
            approval.updated_at = now
            approval.decided_at = now
            approval.decision_comment = comment
            if status == "approved":
                approval.approved_expires_at = self._expiry_timestamp(now)
            else:
                approval.approved_expires_at = None
            await self._persist(approval)
            return approval

    async def _persist(self, approval: ApprovalRecord) -> None:
        await self.file_store.upsert(approval)
        if self.sqlite_store is not None and self._primary is self.sqlite_store:
            await self.sqlite_store.upsert(approval)

    async def _find_matching_pending(
        self,
        *,
        session_id: str,
        kind: ApprovalKind,
        action: BrowserActionDecision,
    ) -> ApprovalRecord | None:
        for approval in await self.list(status="pending", session_id=session_id):
            if approval.kind == kind and self._actions_match(approval.action, action):
                return approval
        return None

    @staticmethod
    def _actions_match(left: BrowserActionDecision, right: BrowserActionDecision) -> bool:
        excluded = {"reason", "confidence"}
        return left.model_dump(exclude=excluded) == right.model_dump(exclude=excluded)

    def _ensure_not_expired(self, approval: ApprovalRecord) -> None:
        if approval.approved_expires_at is None:
            return
        expires_at = self._parse_timestamp(approval.approved_expires_at)
        if expires_at <= datetime.now(UTC):
            raise PermissionError(f"approval {approval.id} has expired")

    def _expiry_timestamp(self, decided_at: str) -> str:
        decided = self._parse_timestamp(decided_at)
        return (decided + self.approval_ttl).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
