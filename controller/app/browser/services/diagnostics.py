from __future__ import annotations

import asyncio
import codecs
import logging
import mimetypes
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...action_errors import SessionNotFoundError
from ...downloads import DownloadCaptureService
from ...pii_scrub import PiiScrubber
from ...utils import UTC, spawn_background_task

logger = logging.getLogger(__name__)

# Larger downloads are fetched from their artifact URL, not read into a model's context.
DOWNLOAD_READ_MAX_BYTES = 10 * 1024 * 1024

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...browser_manager import BrowserSession


class BrowserDiagnosticsService:
    """Encapsulates diagnostics helpers and download persistence hooks."""

    def __init__(self, manager: Any, pii_scrubber: PiiScrubber, download_capture: DownloadCaptureService) -> None:
        self.manager = manager
        self.pii_scrubber = pii_scrubber
        self.download_capture = download_capture

    async def get_console_messages(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            messages = session.console_messages[-limit:]
            if self.pii_scrubber.console_enabled:
                messages, hits = self.pii_scrubber.console(messages)
                if hits and self.pii_scrubber.audit_report:
                    await self.manager.audit.append(
                        event_type="pii_redaction",
                        status="ok",
                        action="console_scrub",
                        session_id=session_id,
                        details=self.pii_scrubber.build_audit_report(session_id, "console", hits),
                    )
            return {
                "session": await self.manager._session_summary(session),
                "items": messages,
            }

    async def get_page_errors(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            return {
                "session": await self.manager._session_summary(session),
                "items": session.page_errors[-limit:],
            }

    async def get_request_failures(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            return {
                "session": await self.manager._session_summary(session),
                "items": session.request_failures[-limit:],
            }

    async def get_network_log(
        self,
        session_id: str,
        *,
        limit: int = 100,
        method: str | None = None,
        url_contains: str | None = None,
    ) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            inspector = session.network_inspector
            if inspector is None:
                return {
                    "session": await self.manager._session_summary(session),
                    "enabled": False,
                    "entries": [],
                    "summary": {},
                }
            return {
                "session": await self.manager._session_summary(session),
                "enabled": True,
                "entries": inspector.entries(limit=limit, method=method, url_contains=url_contains),
                "summary": inspector.summary(),
            }

    def attach_page_listeners(self, page: "Page", session: "BrowserSession") -> None:
        if not hasattr(page, "on"):
            return
        if page in session.attached_pages:
            return
        session.attached_pages.add(page)

        page.on(
            "console",
            lambda message: self._bounded_append(
                session.console_messages,
                {
                    "type": message.type,
                    "text": message.text,
                    "location": message.location,
                },
            ),
        )
        page.on("pageerror", lambda error: self._bounded_append(session.page_errors, str(error)))
        page.on(
            "requestfailed",
            lambda request: self._bounded_append(
                session.request_failures,
                {
                    "url": request.url,
                    "method": request.method,
                    "failure": str(request.failure) if request.failure else None,
                },
            ),
        )
        page.on("download", lambda download: spawn_background_task(self.manager._handle_download(session, download)))
        page.on("close", lambda _page: self._on_page_closed(session, page))

    @staticmethod
    def _on_page_closed(session: "BrowserSession", page: "Page") -> None:
        """Move the session to another open tab when its active tab closes itself.

        A page that called window.close() (a sign-in popup the agent had switched
        to, say) left session.page pointing at a closed page: every later action
        failed and the session read as interrupted while its other tabs were fine.
        """
        if session.page is not page:
            return
        context = getattr(session, "context", None)
        remaining = [p for p in getattr(context, "pages", []) if p is not page and not p.is_closed()]
        if remaining:
            session.page = remaining[-1]

    @staticmethod
    def _bounded_append(items: list[Any], value: Any, limit: int = 50) -> None:
        items.append(value)
        if len(items) > limit:
            del items[: len(items) - limit]

    async def list_downloads(self, session_id: str) -> list[dict[str, Any]]:
        session = self.manager.sessions.get(session_id)
        if session is not None:
            return list(session.downloads)
        try:
            record = await self.manager.session_store.get(session_id)
        except KeyError:
            raise SessionNotFoundError(session_id) from None
        return list(record.downloads)

    async def read_download_text(self, session_id: str, download_id: str | None = None) -> dict[str, Any]:
        """A captured download's contents as text.

        ``download_id`` defaults to the most recent completed download. Only
        files in the session's own downloads directory are read, and only text:
        binary files are refused with their size, type and artifact URL.
        """
        downloads = await self.list_downloads(session_id)
        record = self._select_download(session_id, downloads, download_id)
        downloads_dir = (Path(self.manager.settings.artifact_root) / session_id / "downloads").resolve()
        path = Path(str(record.get("path") or "")).resolve()
        if not path.is_relative_to(downloads_dir):
            logger.warning("refusing to read download %s: %s is outside %s", record.get("id"), path, downloads_dir)
            raise ValueError(f"Download {record.get('id')} is not in this session's downloads directory.")
        try:
            size = path.stat().st_size
        except OSError:
            raise ValueError(f"Download {record.get('id')} ({record.get('filename')}) is no longer on disk.") from None
        content_type = mimetypes.guess_type(path.name)[0]
        described = f"{record.get('filename')} ({content_type or 'unknown type'}, {size:,} bytes)"
        if size > DOWNLOAD_READ_MAX_BYTES:
            raise ValueError(
                f"Download {described} is larger than {DOWNLOAD_READ_MAX_BYTES:,} bytes; fetch it from {record.get('url')}."
            )
        data = await asyncio.to_thread(path.read_bytes)
        text, lossy = _decode_text(data, content_type)
        if text is None:
            raise ValueError(f"Download {described} is binary, not text; fetch it from {record.get('url')}.")
        return {
            "download_id": record.get("id"),
            "filename": record.get("filename"),
            "source_url": record.get("source_url"),
            "url": record.get("url"),
            "content_type": content_type,
            "size_bytes": size,
            "lossy": lossy,
            "text": text,
        }

    @staticmethod
    def _select_download(session_id: str, downloads: list[dict[str, Any]], download_id: str | None) -> dict[str, Any]:
        if download_id is None:
            completed = [item for item in downloads if item.get("status") == "completed"]
            if not completed:
                raise ValueError(f"Session {session_id} has no completed downloads.")
            return completed[-1]
        for item in downloads:
            if item.get("id") == download_id:
                if item.get("status") != "completed":
                    raise ValueError(
                        f"Download {download_id} did not complete ({item.get('failure') or item.get('status')})."
                    )
                return item
        known = ", ".join(str(item.get("id")) for item in downloads[-10:]) or "none"
        raise ValueError(f"No download {download_id} in session {session_id}. Recent downloads: {known}.")

    async def handle_download(self, session: "BrowserSession", download: Any) -> None:
        record = await self.download_capture.capture(session, download)
        await self.manager.audit.append(
            event_type="download_captured",
            status=record["status"],
            action="download",
            session_id=session.id,
            details={"filename": record["filename"], "url": record["url"], "failure": record["failure"]},
        )
        if session.id in self.manager.sessions:
            try:
                await self.manager._persist_session(session, status="active")
            except Exception as exc:
                logger.warning(
                    "failed to persist download metadata for session %s: %s",
                    session.id,
                    exc,
                )

    async def screenshot_diff(self, session_id: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            artifact_dir = session.artifact_dir
            prior_shots = sorted(
                [path for path in artifact_dir.glob("*.png") if "diff-b" not in path.name],
                key=lambda path: path.stat().st_mtime,
            )

            new_shot = await self.manager._capture_screenshot(session, "diff-b")

            if not prior_shots:
                baseline_path = artifact_dir / "screenshot-baseline.png"
                shutil.copy2(new_shot["path"], str(baseline_path))
                return {
                    "baseline_captured": True,
                    "baseline_url": f"/artifacts/{session_id}/screenshot-baseline.png",
                    "message": "Baseline saved. Navigate to a new state and call compare again to see the diff.",
                }

            prev_path = prior_shots[-1]
            prev_url = f"/artifacts/{session_id}/{prev_path.name}"
            return await asyncio.to_thread(
                self.compute_diff,
                str(prev_path),
                new_shot["path"],
                prev_url,
                new_shot["url"],
                session.artifact_dir,
            )

    @staticmethod
    def compute_diff(
        a_path: str,
        b_path: str,
        a_url: str,
        b_url: str,
        artifact_dir: Path,
    ) -> dict[str, Any]:
        try:
            from PIL import Image, ImageChops  # type: ignore[import]

            img_a = Image.open(a_path).convert("RGB")
            img_b = Image.open(b_path).convert("RGB")

            if img_a.size != img_b.size:
                img_b = img_b.resize(img_a.size, Image.LANCZOS)

            diff = ImageChops.difference(img_a, img_b)
            total_pixels = img_a.width * img_a.height

            data = diff.tobytes()
            changed = sum(
                1 for index in range(0, len(data), 3) if data[index] > 8 or data[index + 1] > 8 or data[index + 2] > 8
            )
            changed_pct = round(changed / total_pixels * 100, 4) if total_pixels > 0 else 0.0

            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            diff_filename = f"{ts}-diff.png"
            diff_path = artifact_dir / diff_filename
            diff.save(str(diff_path))
            diff_url = f"/artifacts/{artifact_dir.name}/{diff_filename}"

            return {
                "changed_pixels": changed,
                "changed_pct": changed_pct,
                "diff_url": diff_url,
                "diff_path": str(diff_path),
                "a_url": a_url,
                "b_url": b_url,
                "width": img_a.width,
                "height": img_a.height,
            }
        except Exception as exc:
            logger.warning("screenshot diff failed: %s", exc)
            return {
                "error": "screenshot_diff_failed",
                "changed_pixels": -1,
                "changed_pct": -1.0,
                "diff_url": None,
                "diff_path": None,
                "a_url": a_url,
                "b_url": b_url,
                "width": 0,
                "height": 0,
            }


_TEXTUAL_APPLICATION_TYPES = frozenset(
    {
        "application/javascript",
        "application/json",
        "application/sql",
        "application/toml",
        "application/x-sh",
        "application/x-yaml",
        "application/xml",
        "application/yaml",
    }
)


def _is_textual_type(content_type: str) -> bool:
    return (
        content_type.startswith("text/")
        or content_type in _TEXTUAL_APPLICATION_TYPES
        or content_type.endswith(("+json", "+xml"))
    )


def _decode_text(data: bytes, content_type: str | None) -> tuple[str | None, bool]:
    """(text, lossy), or (None, False) for binary data.

    A file whose extension names a binary type (pdf, xlsx, png, zip ...) is
    binary whatever its first bytes are. Otherwise UTF-8 (with or without a
    BOM) and BOM-marked UTF-16 decode exactly; anything else without a NUL byte
    is text in some legacy encoding, decoded with replacement characters and
    reported as lossy rather than refused.
    """
    if content_type is not None and not _is_textual_type(content_type):
        return None, False
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16"), False
        except UnicodeDecodeError:
            return None, False
    try:
        return data.decode("utf-8-sig"), False
    except UnicodeDecodeError:
        pass
    if b"\x00" in data[:8192]:
        return None, False
    return data.decode("utf-8", errors="replace"), True
