from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable

from .config import Settings
from .utils import UTC

if TYPE_CHECKING:
    from .approvals import ApprovalStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CleanupStats:
    root: str
    retention_hours: float
    deleted_files: int = 0
    deleted_dirs: int = 0
    bytes_reclaimed: int = 0
    skipped_recent: int = 0
    skipped_protected: int = 0
    # Entries the sweep could not stat or remove (a root-owned leftover, say): counted, logged, and passed over.
    errors: int = 0


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
        approval_store: ApprovalStore | None = None,
    ) -> None:
        self.settings = settings
        self.session_provider = session_provider
        # Its decided approvals past APPROVAL_RETENTION_DAYS go in each sweep (ApprovalStore.prune_decided).
        self.approval_store = approval_store
        self._task: asyncio.Task | None = None
        # CLEANUP_ON_STARTUP's sweep, run in the background after startup (see startup()).
        self._startup_task: asyncio.Task | None = None
        # Set on shutdown: the walk runs in a thread that cancelling its task cannot stop, so it checks this.
        self._abort = threading.Event()
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_report: CleanupReport | None = None
        # Set once the convergence harness is initialized (after startup); its old runs go in each sweep.
        self.harness_service = None
        # Set by app.main: writes the refused takeover-link calls that were only counted, once their window is over
        # (routes/sessions.py). Run in each sweep; it logs its own failures and raises none.
        self.write_held_refusals: Callable[[], Awaitable[None]] | None = None

    @property
    def last_report(self) -> dict | None:
        return self._last_report.model_dump() if self._last_report is not None else None

    async def startup(self) -> None:
        self._stop_event.clear()
        self._abort.clear()
        if self.settings.cleanup_on_startup:
            # In the background: awaited here, the sweep held the lifespan (and so /readyz and the API) for ~150 s
            # over 20,000 files on a Docker Desktop bind mount. It cannot race a session that starts meanwhile: it
            # protects every live session's directories, takes only entries older than the retention cutoff (a new
            # session's are not), and shares the loop's lock.
            self._startup_task = asyncio.create_task(self._startup_sweep(), name="maintenance-startup-sweep")
        if self.settings.cleanup_interval_seconds > 0:
            self._task = asyncio.create_task(self._run_loop(), name="maintenance-cleanup-loop")

    async def _startup_sweep(self) -> None:
        logger.info("startup cleanup sweep started")
        started = time.monotonic()
        try:
            report = await self.run_cleanup()
        except asyncio.CancelledError:
            logger.info("startup cleanup sweep cancelled")
            raise
        except Exception:
            logger.exception("startup cleanup sweep failed")
            return
        roots = report["roots"]
        logger.info(
            "startup cleanup sweep finished in %.1fs: %d files and %d dirs removed, %d recent and %d protected kept, "
            "%d error(s)",
            time.monotonic() - started,
            sum(item["deleted_files"] for item in roots),
            sum(item["deleted_dirs"] for item in roots),
            sum(item["skipped_recent"] for item in roots),
            sum(item["skipped_protected"] for item in roots),
            sum(item.get("errors", 0) for item in roots),
        )

    async def shutdown(self) -> None:
        self._stop_event.set()
        self._abort.set()
        if self._startup_task is not None:
            self._startup_task.cancel()
            try:
                await self._startup_task
            except asyncio.CancelledError:
                pass
            self._startup_task = None
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
                # The walk and the unlinks are blocking filesystem calls (13 s
                # for 6000 files on NTFS); on the loop they froze every
                # session and request for the whole sweep.
                reports.append(await asyncio.to_thread(self._cleanup_root, Path(root), retention, protected_roots))
            await self._prune_harness()
            await self._prune_approvals()
            if self.write_held_refusals is not None:
                await self.write_held_refusals()
            completed = datetime.now(UTC)
            self._last_report = CleanupReport(
                started_at=started.isoformat().replace("+00:00", "Z"),
                completed_at=completed.isoformat().replace("+00:00", "Z"),
                roots=reports,
            )
            return self._last_report.model_dump()

    async def _prune_harness(self) -> None:
        service = self.harness_service
        if service is None:
            return
        days = getattr(self.settings, "harness_retention_days", 0)
        if days <= 0:
            return
        try:
            pruned = await asyncio.to_thread(service.store.prune, days)
        except Exception:
            logger.exception("harness retention sweep failed")
            return
        if pruned:
            logger.info("harness retention sweep removed %d run/candidate directories", pruned)

    async def _prune_approvals(self) -> None:
        """The approval retention pass, which logs its own failure and raises none."""
        if self.approval_store is None:
            return
        # An operator's rejection in a session still open keeps refusing an agent's approval of that action there.
        open_sessions = {getattr(session, "id", None) for session in self.session_provider()}
        await self.approval_store.prune_decided(open_sessions=open_sessions - {None})

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.settings.cleanup_interval_seconds)
            except asyncio.TimeoutError:
                try:
                    await self.run_cleanup()
                except Exception:
                    # A single failed sweep must not kill the loop. Previously any
                    # exception here ended the task silently and nothing observed
                    # it, so artifacts/uploads/auth grew forever with only a stale
                    # last_report as the clue. The stat race below was a live
                    # trigger for exactly this.
                    logger.exception("cleanup sweep failed; the maintenance loop continues")

    def _cleanup_roots(self) -> list[tuple[str, float]]:
        return [
            (self.settings.artifact_root, self.settings.artifact_retention_hours),
            (self.settings.upload_root, self.settings.upload_retention_hours),
            (self.settings.auth_root, self.settings.auth_retention_hours),
        ]

    def _protected_roots(self) -> list[Path]:
        protected: list[Path] = [Path(self.settings.auth_root).resolve() / "profiles"]
        for session in self.session_provider():
            for attr in ("artifact_dir", "upload_dir", "auth_dir"):
                value = getattr(session, attr, None)
                if value is None:
                    continue
                protected.append(Path(value).resolve())
        return protected

    def _sweep_entry(
        self, path: Path, cutoff: float, protected_roots: list[Path], stale_dirs: set[Path], stats: CleanupStats
    ) -> None:
        """One entry of the first pass: an OSError from it is the caller's to count, never the sweep's end."""
        resolved = path.resolve()
        protected_match = self._protected_match(resolved, protected_roots)
        if protected_match is not None:
            if resolved == protected_match:
                stats.skipped_protected += 1
            return
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime >= cutoff:
            stats.skipped_recent += 1
            return
        if path.is_dir():
            stale_dirs.add(path)
        elif path.is_file():
            # Same FileNotFoundError race the mtime stat above already
            # guards: a concurrent writer or another sweep can remove the
            # file between the two stats. Unguarded, this propagated out of
            # run_cleanup and killed the loop permanently.
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                return
            path.unlink(missing_ok=True)
            stats.deleted_files += 1
            stats.bytes_reclaimed += size

    @staticmethod
    def _entry_error(stats: CleanupStats, path: Path, exc: Exception, seen: set[Path]) -> None:
        # Counted once per entry, whichever pass met it (P4#7).
        if path in seen:
            return
        seen.add(path)
        stats.errors += 1
        logger.warning("cleanup sweep could not handle %s: %s", path, getattr(exc, "strerror", None) or exc)

    def _cleanup_root(self, root: Path, retention_hours: float, protected_roots: list[Path]) -> CleanupStats:
        stats = CleanupStats(root=str(root), retention_hours=retention_hours)
        if retention_hours <= 0 or not root.exists():
            return stats

        cutoff = datetime.now(UTC).timestamp() - (retention_hours * 3600)
        # Only directories already stale when the sweep began may be removed.
        # The second pass used to remove any empty directory, however new: a
        # session being created (not yet in session_provider) lost its fresh,
        # still-empty artifact or upload directory, and its later writes failed.
        stale_dirs: set[Path] = set()
        # Listing the tree is most of the cost on a slow mount (a Docker Desktop bind mount), so shutdown is
        # checked while listing too: the thread outlives its cancelled task, and the process waits for it at exit.
        # The sort key stats each entry (is_file), so it is computed here, between abort checks, not inside sorted().
        # resolve() raises RuntimeError on a symlink loop (Python 3.11), so entries catch that as well as OSError.
        failed: set[Path] = set()
        entries: list[tuple[tuple[bool, int], Path]] = []
        for item in root.rglob("*"):
            if self._abort.is_set():
                logger.info("cleanup walk of %s stopped for shutdown", root)
                return stats
            try:
                is_file = item.is_file()
            except OSError as exc:
                self._entry_error(stats, item, exc, failed)
                continue
            entries.append(((is_file, len(item.parts)), item))
        entries.sort(key=lambda pair: pair[0])
        for _, path in entries:
            if self._abort.is_set():
                logger.info("cleanup walk of %s stopped for shutdown", root)
                return stats
            if path.name == ".gitkeep":
                continue
            try:
                self._sweep_entry(path, cutoff, protected_roots, stale_dirs, stats)
            except (OSError, RuntimeError) as exc:
                self._entry_error(stats, path, exc, failed)

        directories: list[Path] = []
        for item in root.rglob("*"):
            if self._abort.is_set():
                logger.info("cleanup walk of %s stopped for shutdown", root)
                return stats
            try:
                if item.is_dir():
                    directories.append(item)
            except OSError as exc:
                self._entry_error(stats, item, exc, failed)
        for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            if self._abort.is_set():
                logger.info("cleanup walk of %s stopped for shutdown", root)
                return stats
            if path not in stale_dirs:
                continue
            try:
                resolved = path.resolve()
            except (OSError, RuntimeError) as exc:
                self._entry_error(stats, path, exc, failed)
                continue
            if self._protected_match(resolved, protected_roots) is not None:
                continue
            try:
                # rmdir refuses a non-empty directory, so a file written since
                # the first pass keeps it. (rmdir used to run inside the
                # StopIteration handler, where its OSError escaped the sweep.)
                path.rmdir()
            except OSError:
                continue
            stats.deleted_dirs += 1
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
