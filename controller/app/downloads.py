from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from .artifacts import SessionArtifactService
from .utils import utc_now


class DownloadCaptureService:
    def __init__(self, artifacts: SessionArtifactService) -> None:
        self.artifacts = artifacts

    async def capture(self, session: Any, download: Any) -> dict[str, Any]:
        suggested = Path(str(getattr(download, "suggested_filename", "") or "")).name
        if suggested in {"", ".", ".."}:
            # The site picks this name. "." or ".." named the downloads
            # directory or its parent, and the save failed.
            suggested = f"download-{uuid4().hex}"
        destination = self._reserve(session.artifact_dir / "downloads", suggested)

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

    @staticmethod
    def _reserve(directory: Path, filename: str) -> Path:
        """Claim a file name no other download is using.

        The name is claimed by creating the file, not by checking that it is
        absent: two downloads of one name (a page fetching report.csv twice)
        were both handed the same path, and the second save replaced the first.
        """
        directory.mkdir(parents=True, exist_ok=True)
        candidate = directory / filename
        while True:
            try:
                candidate.open("xb").close()
                return candidate
            except FileExistsError:
                stem = Path(filename).stem
                candidate = directory / f"{stem}-{uuid4().hex[:8]}{Path(filename).suffix}"
            except OSError:
                # A name the filesystem refuses (too long, say) failed the save
                # before; a generated one keeps the download.
                filename = f"download-{uuid4().hex}"
                candidate = directory / filename

    @staticmethod
    def _bounded_append(items: list[Any], value: Any, limit: int) -> None:
        items.append(value)
        if len(items) > limit:
            del items[: len(items) - limit]
