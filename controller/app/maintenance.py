from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable

from .config import Settings


@dataclass(slots=True)
class CleanupStats:
    root: str
    retention_hours: float
    deleted_files: int = 0
    deleted_dirs: int = 0
    bytes_reclaimed: int = 0
    skipped_recent: int = 0
    skipped_protected: int = 0


@dataclass(slots=True)
class CleanupReport:
    started_at: str
    completed_at: str
    roots: list[CleanupStats] = field(default_factory=list)

    def model_dump(self) -> dict:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "roots": [asdict(item) for item in self.roots],
        }


class MaintenanceService:
    def __init__(
        self,
        settings: Settings,
        *,
        session_provider: Callable[[], Iterable[object]],
    ) -> None:
        self.settings = settings
        self.session_provider = session_provider
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_report: CleanupReport | None = None

    @property
    def last_report(self) -> dict | None:
        return self._last_report.model_dump() if self._last_report is not None else None

    async def startup(self) -> None:
        self._stop_event.clear()
        if self.settings.cleanup_on_startup:
            await self.run_cleanup()
        if self.settings.cleanup_interval_seconds > 0:
            self._task = asyncio.create_task(self._run_loop(), name="maintenance-cleanup-loop")

    async def shutdown(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_cleanup(self) -> dict:
        async with self._lock:
            started = datetime.now(UTC)
            protected_roots = self._protected_roots()
            reports = []
            for root, retention in self._cleanup_roots():
                reports.append(self._cleanup_root(Path(root), retention, protected_roots))
            completed = datetime.now(UTC)
            self._last_report = CleanupReport(
                started_at=started.isoformat().replace("+00:00", "Z"),
                completed_at=completed.isoformat().replace("+00:00", "Z"),
                roots=reports,
            )
            return self._last_report.model_dump()

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.settings.cleanup_interval_seconds)
            except asyncio.TimeoutError:
                await self.run_cleanup()

    def _cleanup_roots(self) -> list[tuple[str, float]]:
        return [
            (self.settings.artifact_root, self.settings.artifact_retention_hours),
            (self.settings.upload_root, self.settings.upload_retention_hours),
            (self.settings.auth_root, self.settings.auth_retention_hours),
        ]

    def _protected_roots(self) -> list[Path]:
        protected: list[Path] = []
        for session in self.session_provider():
            for attr in ("artifact_dir", "upload_dir", "auth_dir"):
                value = getattr(session, attr, None)
                if value is None:
                    continue
                protected.append(Path(value).resolve())
        return protected

    def _cleanup_root(self, root: Path, retention_hours: float, protected_roots: list[Path]) -> CleanupStats:
        stats = CleanupStats(root=str(root), retention_hours=retention_hours)
        if retention_hours <= 0 or not root.exists():
            return stats

        cutoff = datetime.now(UTC).timestamp() - (retention_hours * 3600)
        for path in sorted(root.rglob("*"), key=lambda item: (item.is_file(), len(item.parts))):
            if path.name == ".gitkeep":
                continue
            resolved = path.resolve()
            protected_match = self._protected_match(resolved, protected_roots)
            if protected_match is not None:
                if resolved == protected_match:
                    stats.skipped_protected += 1
                continue
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue
            if mtime >= cutoff:
                stats.skipped_recent += 1
                continue
            if path.is_file():
                size = path.stat().st_size
                path.unlink(missing_ok=True)
                stats.deleted_files += 1
                stats.bytes_reclaimed += size

        for path in sorted((item for item in root.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
            resolved = path.resolve()
            if self._protected_match(resolved, protected_roots) is not None:
                continue
            try:
                next(path.iterdir())
            except StopIteration:
                path.rmdir()
                stats.deleted_dirs += 1
            except (FileNotFoundError, OSError):
                continue
        return stats

    @staticmethod
    def _is_relative_to(path: Path, other: Path) -> bool:
        try:
            path.relative_to(other)
            return True
        except ValueError:
            return False

    @classmethod
    def _protected_match(cls, path: Path, protected_roots: list[Path]) -> Path | None:
        for protected in protected_roots:
            if cls._is_relative_to(path, protected):
                return protected
        return None
