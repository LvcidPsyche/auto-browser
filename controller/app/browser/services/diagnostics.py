from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...browser_scripts import VALIDATION_MESSAGES_SCRIPT
from ...downloads import DownloadCaptureService
from ...pii_scrub import PiiScrubber
from ...utils import UTC, spawn_background_task
from ..tab_diagnostics import bounded_append as _tab_bounded_append
from ..tab_diagnostics import get_buffer
from ..tab_view import tab_id_for, unwrap_session

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from playwright.async_api import Page, Response

    from ...browser_manager import BrowserSession

# A failed response's JSON/text body is capped before it is even decoded --
# this is where a site puts the real reason ("invalid area"), so it is worth
# capturing, but never the whole body of an oversized response.
_RESPONSE_BODY_CAP_BYTES = 16384
_RESPONSE_BODY_SNIPPET_CHARS = 2000
# Only XHR/fetch responses are diagnostic-relevant here -- a failed image or a
# third-party script 404 is noise an employee cannot act on; a failed document
# navigation already shows up as its own console/page error.
_DIAGNOSTIC_RESOURCE_TYPES = frozenset({"xhr", "fetch"})


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
        # Listeners outlive the request: bind them to the real session, never
        # to a per-request tab view.
        session = unwrap_session(session)
        if page in session.attached_pages:
            return
        session.attached_pages.add(page)

        page.on("console", lambda message: self._on_console(session, page, message))
        page.on("pageerror", lambda error: self._on_pageerror(session, page, error))
        page.on("requestfailed", lambda request: self._on_request_failed(session, page, request))
        page.on("response", lambda response: spawn_background_task(self._on_response(session, page, response)))
        page.on("download", lambda download: spawn_background_task(self.manager._handle_download(session, download)))
        # Remember the newest file chooser whoever opened it (see
        # FileTransferService.attach): Google Flow's "Upload" opens the native
        # chooser straight from a click, with no <input type=file> left on the
        # page, so an upload after an agent's own click had nothing to fill.
        page.on("filechooser", lambda chooser: setattr(session, "pending_file_chooser", (chooser, time.monotonic())))
        # Dialog + popup listeners. Registering a dialog listener at all is what
        # stops Playwright auto-dismissing every alert/confirm/prompt -- including
        # the ones the owner sees while browsing by hand. See dialogs.py.
        dialogs = getattr(self.manager, "dialogs", None)
        if dialogs is not None:
            dialogs.attach(page, session)

    @staticmethod
    def _bounded_append(items: list[Any], value: Any, limit: int = 50) -> None:
        items.append(value)
        if len(items) > limit:
            del items[: len(items) - limit]

    # ── Per-page listeners: feed both the session-wide lists (used by
    # observe(), unchanged) and this page's own tab buffer (used by
    # get_diagnostics() / build_digest()) ──────────────────────────────────

    def _on_console(self, session: "BrowserSession", page: "Page", message: Any) -> None:
        entry = {"type": message.type, "text": message.text, "location": message.location}
        self._bounded_append(session.console_messages, entry)
        _tab_bounded_append(get_buffer(session, tab_id_for(session, page)).console, entry)

    def _on_pageerror(self, session: "BrowserSession", page: "Page", error: Any) -> None:
        text = str(error)
        self._bounded_append(session.page_errors, text)
        _tab_bounded_append(get_buffer(session, tab_id_for(session, page)).page_errors, {"text": text})

    def _on_request_failed(self, session: "BrowserSession", page: "Page", request: Any) -> None:
        entry = {
            "url": request.url,
            "method": request.method,
            "failure": str(request.failure) if request.failure else None,
        }
        self._bounded_append(session.request_failures, entry)
        _tab_bounded_append(get_buffer(session, tab_id_for(session, page)).request_failures, entry)

    async def _on_response(self, session: "BrowserSession", page: "Page", response: "Response") -> None:
        """A failed XHR/fetch response (status >= 400): capture its JSON/text
        body, capped and PII-scrubbed -- this is where a site states the real
        reason ("invalid area"), which a bare status code never tells."""
        try:
            status = response.status
            if status < 400:
                return
            request = getattr(response, "request", None)
            resource_type = (getattr(request, "resource_type", "") or "").lower()
            if resource_type not in _DIAGNOSTIC_RESOURCE_TYPES:
                return
            headers = response.headers or {}
            content_type = headers.get("content-type", "") or ""
            body_snippet: str | None = None
            if any(marker in content_type.lower() for marker in ("json", "text")):
                try:
                    raw = await response.body()
                except Exception as exc:
                    logger.debug("diagnostics: could not read response body for %s: %s", response.url, exc)
                    raw = None
                if raw:
                    text = raw[:_RESPONSE_BODY_CAP_BYTES].decode("utf-8", errors="replace")
                    if self.pii_scrubber is not None:
                        try:
                            scrubbed, _hits = self.pii_scrubber.network_body(text, content_type)
                            if isinstance(scrubbed, str):
                                text = scrubbed
                        except Exception as exc:
                            logger.debug("diagnostics: response body scrub failed: %s", exc)
                    body_snippet = text[:_RESPONSE_BODY_SNIPPET_CHARS]
            entry = {
                "url": response.url,
                "method": getattr(request, "method", None),
                "status": status,
                "content_type": content_type,
                "body": body_snippet,
            }
            _tab_bounded_append(get_buffer(session, tab_id_for(session, page)).response_errors, entry)
        except Exception as exc:
            logger.debug("diagnostics: response capture failed: %s", exc)

    async def get_diagnostics(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        """GET /sessions/{id}/diagnostics: the tab-scoped DevTools digest --
        recent console errors/warnings, failed requests with their bodies,
        visible validation messages, and the tab's own url/title."""
        session = await self.manager.get_session(session_id)
        async with session.lock:
            return await self.manager.session_lifecycle.guarded(
                session, self._get_diagnostics_locked(session, limit=limit),
                what="diagnostics", timeout=self.manager.settings.browser_call_timeout_seconds,
            )

    async def _get_diagnostics_locked(self, session: "BrowserSession", *, limit: int) -> dict[str, Any]:
        digest = await self.build_digest(session, limit=limit)
        digest["session"] = await self.manager._session_summary(session)
        return digest

    async def build_digest(self, session: "BrowserSession", *, limit: int = 5) -> dict[str, Any]:
        """The digest used both by GET /diagnostics (generous limit) and by
        the automatic error-payload attachment on a failed action (a small
        limit -- see app/actions/pipeline.py)."""
        real = unwrap_session(session)
        page = session.page
        tab_id = getattr(session, "tab_id", None) or tab_id_for(real, page)
        buf = real.tab_diagnostics.get(tab_id)
        console_errors: list[dict[str, Any]] = []
        page_errors: list[Any] = []
        failed_requests: list[dict[str, Any]] = []
        if buf is not None:
            console_errors = [m for m in buf.console if m.get("type") in ("error", "warning")][-limit:]
            page_errors = list(buf.page_errors[-limit:])
            failed_requests = (buf.request_failures + buf.response_errors)[-limit:]
        if console_errors and self.pii_scrubber.console_enabled:
            try:
                console_errors, hits = self.pii_scrubber.console(console_errors)
                if hits and self.pii_scrubber.audit_report:
                    await self.manager.audit.append(
                        event_type="pii_redaction",
                        status="ok",
                        action="diagnostics_scrub",
                        session_id=getattr(real, "id", ""),
                        details=self.pii_scrubber.build_audit_report(getattr(real, "id", ""), "console", hits),
                    )
            except Exception as exc:
                logger.debug("diagnostics: console scrub failed: %s", exc)
        validation_messages = await self._validation_messages(page, limit=limit)
        try:
            title = await page.title()
        except Exception:
            title = ""
        return {
            "url": getattr(page, "url", ""),
            "title": title,
            "tab_id": tab_id,
            "console_errors": console_errors,
            "page_errors": page_errors,
            "failed_requests": failed_requests,
            "validation_messages": validation_messages,
        }

    async def _validation_messages(self, page: "Page", *, limit: int = 20) -> list[dict[str, Any]]:
        try:
            result = await page.evaluate(VALIDATION_MESSAGES_SCRIPT, max(1, min(limit, 50)))
        except Exception as exc:
            logger.debug("diagnostics: validation message scan failed: %s", exc)
            return []
        return result if isinstance(result, list) else []

    async def list_downloads(self, session_id: str) -> list[dict[str, Any]]:
        session = self.manager.sessions.get(session_id)
        if session is not None:
            return list(session.downloads)
        record = await self.manager.session_store.get(session_id)
        return list(record.downloads)

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
