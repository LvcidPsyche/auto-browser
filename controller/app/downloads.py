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
        suggested = Path(str(getattr(download, "suggested_filename", "") or f"download-{uuid4().hex}")).name
        destination = session.artifact_dir / "downloads" / suggested
        if destination.exists():
            destination = destination.with_name(f"{destination.stem}-{uuid4().hex[:8]}{destination.suffix}")

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
    def _bounded_append(items: list[Any], value: Any, limit: int) -> None:
        items.append(value)
        if len(items) > limit:
            del items[: len(items) - limit]
