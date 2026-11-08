from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import UTC, without_lone_surrogates


class _ThreadWrite:
    """Which side removes the file a worker thread writes for a caller that gave up: the thread, once it ends after the
    caller's cancel, or the caller, when the thread had ended before it. One lock decides, as
    session_isolation._ProvisionHandoff's does for a provision."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.abandoned = False
        self.ended = False

    def run(self, function: Callable[..., Any], *args: Any) -> Any:
        """In the thread: ``function(*args)``, then the file goes if the caller gave up meanwhile."""
        try:
            return function(*args)
        finally:
            with self.lock:
                self.ended = True
                if self.abandoned:
                    Path(self.path).unlink(missing_ok=True)

    def abandon(self) -> None:
        """In the cancelled caller: the thread removes the file when it ends, or it goes now if the thread has ended."""
        with self.lock:
            self.abandoned = True
            if self.ended:
                Path(self.path).unlink(missing_ok=True)


async def in_thread_writing(path: str, function: Callable[..., Any], *args: Any) -> Any:
    """``function(*args)``, which writes ``path``, in a worker thread. A cancel does not stop the thread, so on one the
    file goes once the thread is done, and the thread removes it itself: deleted at the cancel, it was written again
    after it (review of c4), and deleted when the task awaiting the thread ended, it was written after a shutdown,
    which cancels that task while the thread still runs (review of c4b)."""
    write = _ThreadWrite(path)
    try:
        return await asyncio.to_thread(write.run, function, *args)
    except asyncio.CancelledError:
        write.abandon()
        raise


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
        # The image's bytes, written here: Playwright writes the file given as ``path`` in an executor thread of its
        # own, which nothing here can follow, so a capture that a shutdown cancelled had its file removed before that
        # thread wrote it (review of c4b). PNG, as the name a capture still to be redacted goes to is not .png (ACT-33).
        data = await session.page.screenshot(full_page=False, type="png")
        await in_thread_writing(target["path"], Path(target["path"]).write_bytes, data)
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
        # A lone surrogate (UTF-8 cannot encode one) is written as U+FFFD, or the write fails after the action ran.
        line = without_lone_surrogates(json.dumps(payload, ensure_ascii=False))
        await asyncio.to_thread(self.append_text, path, line + "\n")

    @staticmethod
    def append_text(path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    @staticmethod
    def _safe_label(label: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip(".-")
        return safe[:120] or "screenshot"
