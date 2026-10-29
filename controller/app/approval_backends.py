"""Where approval records are kept: one JSON file each under APPROVAL_ROOT, or a SQLite table."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .models import ApprovalRecord, ApprovalStatus
from .sqlite_utils import connect_sqlite
from .utils import PRIVATE_FILE_MODE, atomic_write_text, record_path

logger = logging.getLogger(__name__)


class UnreadableRecord(KeyError):
    """A stored record that no longer validates: read as an unknown id, so nothing can run it, though it is
    there, and the arguments an approve of the mended record reads are kept (the startup sweep)."""


def _valid_record(approval_id: str, payload: str) -> ApprovalRecord | None:
    """The record stored as ``payload``, or None, with a warning naming it, when it no longer validates.

    An earlier version stored what this one refuses (a navigate approval to a URL whose host a browser reads
    otherwise): one such record failed the whole listing, and the SQLite catch-up with it at every start.
    """
    try:
        return ApprovalRecord.model_validate_json(payload)
    except ValueError as exc:
        # Where and what kind of failure only: pydantic's messages and input preview quote the stored record
        # (a navigate URL, a token in its query).
        failures = (
            [
                f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}"
                for error in exc.errors(include_url=False, include_context=False, include_input=False)
            ]
            if isinstance(exc, ValidationError)
            else [type(exc).__name__]
        )
        logger.warning(
            "approval %s is left out: its stored record no longer validates (%s)", approval_id, "; ".join(failures)
        )
        return None


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

    async def remove(self, approval_id: str) -> None:
        """Remove a record, if there is one: a write SQLite refused, of an approval SQLite does not hold."""
        await asyncio.to_thread(self._path(approval_id).unlink, missing_ok=True)

    def _list_sync(
        self,
        status: ApprovalStatus | None,
        session_id: str | None,
    ) -> list[ApprovalRecord]:
        approvals: list[ApprovalRecord] = []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            try:
                payload = path.read_text(encoding="utf-8")
            except (OSError, ValueError) as exc:  # gone since the glob, or not UTF-8
                logger.debug("skipping unreadable approval file %s: %s", path.name, exc)
                continue
            # As SQLite's listing: a warning naming the record, without what it holds.
            approval = _valid_record(path.stem, payload)
            if approval is None:
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
        approval = _valid_record(approval_id, path.read_text(encoding="utf-8"))
        if approval is None:
            raise UnreadableRecord(approval_id)
        return approval

    def _write_sync(self, approval: ApprovalRecord) -> None:
        path = self._path(approval.id)
        atomic_write_text(path, approval.model_dump_json(indent=2), mode=PRIVATE_FILE_MODE)

    def _path(self, approval_id: str) -> Path:
        return record_path(self.root, approval_id, ".json")

    # A tool call's whole arguments, beside its record: not *.json, so a listing never reads them.
    async def write_arguments(self, approval_id: str, arguments: str) -> None:
        await asyncio.to_thread(atomic_write_text, self._arguments_path(approval_id), arguments, mode=PRIVATE_FILE_MODE)

    async def read_arguments(self, approval_id: str) -> str:
        return await asyncio.to_thread(self._read_arguments_sync, approval_id)

    async def delete_arguments(self, approval_id: str) -> None:
        await asyncio.to_thread(self._arguments_path(approval_id).unlink, missing_ok=True)

    async def has_arguments(self, approval_id: str) -> bool:
        return await asyncio.to_thread(self._arguments_path(approval_id).exists)

    async def arguments_ids(self) -> list[str]:
        """The approvals whose arguments are kept here."""
        return await asyncio.to_thread(self._arguments_ids_sync)

    def _arguments_ids_sync(self) -> list[str]:
        ids = []
        for path in self.root.glob("*.arguments"):
            try:
                if self._arguments_path(path.stem) == path:
                    ids.append(path.stem)
            except KeyError:
                continue  # not a name this store writes
        return sorted(ids)

    def _read_arguments_sync(self, approval_id: str) -> str:
        path = self._arguments_path(approval_id)
        if not path.exists():
            raise KeyError(approval_id)
        return path.read_text(encoding="utf-8")

    def _arguments_path(self, approval_id: str) -> Path:
        return record_path(self.root, approval_id, ".arguments")


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
        with closing(self._connect()) as conn, conn:
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
        query = "SELECT id, payload FROM approvals WHERE 1=1"
        params: list[str] = []
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        query += " ORDER BY created_at DESC, id DESC"
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(query, params).fetchall()
        return [record for row in rows if (record := _valid_record(row[0], row[1])) is not None]

    def _get_sync(self, approval_id: str) -> ApprovalRecord:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT payload FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(approval_id)
        approval = _valid_record(approval_id, row[0])
        if approval is None:
            raise UnreadableRecord(approval_id)
        return approval

    def _upsert_sync(self, approval: ApprovalRecord) -> None:
        with closing(self._connect()) as conn, conn:
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
        return connect_sqlite(self.db_path, timeout=10, row_factory=sqlite3.Row)
