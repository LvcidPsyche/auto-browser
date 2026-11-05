from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from playwright.async_api import Error as PlaywrightError

from .artifacts import SessionArtifactService
from .network_inspector import url_origin
from .utils import utc_now

logger = logging.getLogger(__name__)

# Name attempts before `_reserve` gives up. It runs on the event loop.
_RESERVE_ATTEMPTS = 20
DEFAULT_DOWNLOAD_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_DOWNLOADS_MAX_PER_SESSION = 100


class DownloadCaptureService:
    def __init__(
        self,
        artifacts: SessionArtifactService,
        *,
        max_bytes: int = DEFAULT_DOWNLOAD_MAX_BYTES,
        max_per_session: int = DEFAULT_DOWNLOADS_MAX_PER_SESSION,
    ) -> None:
        self.artifacts = artifacts
        self.max_bytes = max_bytes
        self.max_per_session = max_per_session

    async def capture(self, session: Any, download: Any) -> dict[str, Any]:
        suggested = Path(str(getattr(download, "suggested_filename", "") or "")).name
        if suggested in {"", ".", ".."}:
            # The site picks this name. "." or ".." named the downloads
            # directory or its parent, and the save failed.
            suggested = f"download-{uuid4().hex}"
        downloads_dir = session.artifact_dir / "downloads"
        # Counted from the files on disk (the in-memory list is capped), with no await before the name is
        # reserved, so concurrent downloads cannot all pass the check.
        if self._saved_count(downloads_dir) >= self.max_per_session:
            return await self.refuse(
                session,
                download,
                f"session already holds {self.max_per_session} downloads (DOWNLOADS_MAX_PER_SESSION)",
            )
        destination = self._reserve(downloads_dir, suggested)

        failure: str | None = None
        status = "completed"
        try:
            await download.save_as(str(destination))
            if hasattr(download, "failure"):
                failure = await download.failure()
        except Exception:
            failure = "download_save_failed"
            status = "failed"

        if failure:
            status = "failed"
        elif destination.stat().st_size > self.max_bytes:
            destination.unlink(missing_ok=True)
            return await self.refuse(
                session,
                download,
                f"download is larger than {self.max_bytes} bytes (DOWNLOAD_MAX_BYTES) and was deleted",
                cancel=False,
            )

        record = {
            "id": uuid4().hex[:12],
            "timestamp": utc_now(),
            "status": status,
            "filename": destination.name,
            "suggested_filename": suggested,
            "path": str(destination),
            "url": f"/artifacts/{session.id}/downloads/{destination.name}",
            "source_url": getattr(download, "url", None),
            "failure": failure,
        }
        self._bounded_append(session.downloads, record, limit=100)
        await self.artifacts.append_jsonl(session.artifact_dir / "downloads.jsonl", record)
        return record

    async def block(self, session: Any, download: Any, reason: str) -> dict[str, Any]:
        """Cancel a download ALLOWED_HOSTS refuses, and record it with nothing saved.

        The page chose the file's name and its URL's path, so the record keeps the origin only.
        """
        try:
            await download.cancel()
        except PlaywrightError as exc:
            # Only capture() saves a download, so nothing is kept either way.
            logger.debug("could not cancel a refused download for session %s: %s", session.id, exc)
        record = {
            "id": uuid4().hex[:12],
            "timestamp": utc_now(),
            "status": "blocked",
            "filename": None,
            "suggested_filename": None,
            "path": None,
            "url": None,
            "source_url": url_origin(str(getattr(download, "url", "") or "")),
            "failure": reason,
        }
        self._bounded_append(session.downloads, record, limit=100)
        await self.artifacts.append_jsonl(session.artifact_dir / "downloads.jsonl", record)
        return record

    async def refuse(self, session: Any, download: Any, reason: str, *, cancel: bool = True) -> dict[str, Any]:
        """Record a download the caps turned away, with nothing kept."""
        if cancel:
            try:
                await download.cancel()
            except PlaywrightError as exc:
                logger.debug("could not cancel a refused download for session %s: %s", session.id, exc)
        record = {
            "id": uuid4().hex[:12],
            "timestamp": utc_now(),
            "status": "refused",
            "filename": None,
            "suggested_filename": None,
            "path": None,
            "url": None,
            "source_url": url_origin(str(getattr(download, "url", "") or "")),
            "failure": reason,
        }
        self._bounded_append(session.downloads, record, limit=100)
        await self.artifacts.append_jsonl(session.artifact_dir / "downloads.jsonl", record)
        return record

    @staticmethod
    def _saved_count(directory: Path) -> int:
        return sum(1 for _ in directory.iterdir()) if directory.is_dir() else 0

    @staticmethod
    def _reserve(directory: Path, filename: str) -> Path:
        """Claim a file name no other download is using.

        The name is claimed by creating the file, not by checking that it is
        absent: two downloads of one name (a page fetching report.csv twice)
        were both handed the same path, and the second save replaced the first.
        """
        directory.mkdir(parents=True, exist_ok=True)
        candidate = directory / filename
        renamed = False
        for _ in range(_RESERVE_ATTEMPTS):
            try:
                candidate.open("xb").close()
                return candidate
            except FileExistsError:
                stem = Path(filename).stem
                candidate = directory / f"{stem}-{uuid4().hex[:8]}{Path(filename).suffix}"
            except OSError:
                # A name the filesystem refuses (too long, say) failed the save
                # before; a generated one keeps the download. One fallback only:
                # an error that survives it (no space, read-only volume, no
                # permission) is not about the name, so it is raised.
                if renamed:
                    raise
                renamed = True
                filename = f"download-{uuid4().hex}"
                candidate = directory / filename
        raise FileExistsError(f"could not reserve a free name in {directory} after {_RESERVE_ATTEMPTS} attempts")

    @staticmethod
    def _bounded_append(items: list[Any], value: Any, limit: int) -> None:
        items.append(value)
        if len(items) > limit:
            del items[: len(items) - limit]
