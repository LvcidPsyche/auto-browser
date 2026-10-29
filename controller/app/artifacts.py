from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import UTC


class SessionArtifactService:
    def __init__(self, artifact_root: str | Path) -> None:
        self.artifact_root = Path(artifact_root)

    def prepare_session_dir(self, session_id: str) -> Path:
        artifact_dir = self.artifact_root / session_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "downloads").mkdir(parents=True, exist_ok=True)
        return artifact_dir

    def screenshot_target(self, session: Any, label: str) -> dict[str, str]:
        """Where a screenshot labelled ``label`` is written, and its artifact URL."""
        filename = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}Z-{self._safe_label(label)}.png"
        return {"path": str(session.artifact_dir / filename), "url": f"/artifacts/{session.id}/{filename}"}

    async def capture_screenshot(
        self, session: Any, label: str, *, target: dict[str, str] | None = None
    ) -> dict[str, str]:
        target = target or self.screenshot_target(session, label)
        await session.page.screenshot(path=target["path"], full_page=False)
        return dict(target)

    @staticmethod
    def trace_payload(session: Any) -> dict[str, Any]:
        return {
            "trace_path": str(session.trace_path),
            "trace_url": f"/artifacts/{session.id}/{session.trace_path.name}",
            "trace_exists": session.trace_path.exists(),
            "trace_recording": session.trace_recording,
        }

    async def append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False)
        await asyncio.to_thread(self.append_text, path, line + "\n")

    @staticmethod
    def append_text(path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    @staticmethod
    def _safe_label(label: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip(".-")
        return safe[:120] or "screenshot"
