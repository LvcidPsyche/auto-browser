from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from ... import events as _events
from ...action_errors import SCREENSHOT_WITHHELD, BrowserActionError
from ...browser_scripts import INTERACTABLES_SCRIPT, PAGE_SUMMARY_SCRIPT
from ..aria_outline import outline_from_aria_snapshot, unavailable_outline

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...browser_manager import BrowserSession

logger = logging.getLogger(__name__)


async def _gather_settled(*aws: Any) -> list[Any]:
    """Run ``aws`` concurrently and return their results, raising the first error
    only once every one has finished.

    The screenshot is taken in the compositor while the DOM reads run on the
    page, so overlapping them cuts a snapshot by about a fifth (70 ms instead of
    85 ms on a 300-row page, 370 ms instead of 485 ms on a 3,000-row one). Plain
    ``gather`` would return on the first failure and leave the other call
    running against the page after the caller has released the session lock.
    """
    results = await asyncio.gather(*aws, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


def _scrub_disclosure(screenshot: dict[str, Any]) -> dict[str, Any]:
    """The result field that says a screenshot went out without PII redaction.

    Present only when screenshot scrubbing is on but could not run (no OCR);
    `_scrub_screenshot_if_needed` records the reason on the screenshot dict.
    """
    reason = screenshot.get("pii_scrub_skipped")
    return {"screenshot_pii_scrub": {"applied": False, "reason": reason}} if reason else {}


# An outline is a nicety; an observation must not wait long on one.
ARIA_SNAPSHOT_TIMEOUT_MS = 5000

# The share page asks for a frame every few seconds for as long as it is open.
# Each frame is a file, so they are named apart and pruned; a viewer loads its
# frame right after the response that names it, so a minute is ample.
SHARE_FRAME_LABEL = "share-frame"
SHARE_FRAME_KEEP_SECONDS = 60.0


def _prune_share_frames(directory: Path, *, keep: Path) -> None:
    cutoff = time.time() - SHARE_FRAME_KEEP_SECONDS
    try:
        for path in directory.glob(f"*-{SHARE_FRAME_LABEL}.png"):
            if path != keep and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
    except OSError as exc:
        # Housekeeping: a failure must not take the viewer's frame down with it.
        logger.warning("failed to prune share frames in %s: %s", directory, exc)


class BrowserObservationService:
    """Encapsulates observation, screenshot, and trace payload helpers."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._warned_unscrubbed: set[str] = set()

    async def observe(self, session_id: str, limit: int = 40, preset: str | None = None) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            result = await self.observation_payload(session, limit=limit, preset=preset)
            _events.emit_observe(
                session_id,
                result.get("url", ""),
                result.get("title", ""),
                result.get("screenshot_url"),
            )
            return result

    async def share_snapshot(self, session_id: str) -> dict[str, Any]:
        """What the share page renders on each poll: a frame, the URL and the title.

        The page polls every few seconds for as long as it is open, and this used
        to be a full observation: DOM and ARIA reads, OCR and a screenshot kept
        for good, all under the session lock, so a watched session's agent
        waited on the viewer and its artifact directory filled with PNGs. This
        holds the lock for one redacted capture and two reads, and keeps only
        recent frames. It does not emit an observe event or touch the audit log.
        """
        session = await self.manager.get_session(session_id)
        async with session.lock:
            screenshot = await self._capture_screenshot_redacted(session, SHARE_FRAME_LABEL)
            snapshot = {
                "url": session.page.url,
                "title": await session.page.title(),
                "screenshot_path": screenshot["path"],
            }
        await asyncio.to_thread(_prune_share_frames, Path(session.artifact_dir), keep=Path(screenshot["path"]))
        return snapshot

    async def capture_screenshot(self, session_id: str, *, label: str = "manual") -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            screenshot = await self._capture_screenshot_redacted(session, label)
            return {
                "session": await self.manager._session_summary(session),
                "url": session.page.url,
                "screenshot_path": screenshot["path"],
                "screenshot_url": screenshot["url"],
                **_scrub_disclosure(screenshot),
                "takeover_url": self.manager._current_takeover_url(session),
            }

    async def stop_trace(self, session_id: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            await self.stop_trace_recording(session)
            return {
                "session": await self.manager._session_summary(session),
                **self.trace_payload(session),
            }

    async def observation_payload(
        self,
        session: "BrowserSession",
        *,
        limit: int = 40,
        screenshot_label: str = "observe",
        preset: str | None = None,
    ) -> dict[str, Any]:
        self.manager._assert_page_allowed(session)
        if preset is None:
            preset = self.manager.settings.perception_preset_default
        if preset not in ("text", "fast", "normal", "rich"):
            logger.warning("unknown perception preset %r; falling back to 'normal'", preset)
            preset = "normal"

        if preset == "fast":
            screenshot = await self._capture_screenshot_redacted(session, screenshot_label)
            title = await session.page.title()
            tabs = await self.manager.tabs.summaries(session)
            return {
                "session": await self.manager._session_summary(session),
                "url": session.page.url,
                "title": title,
                "active_element": None,
                "text_excerpt": "",
                "dom_outline": {},
                "accessibility_outline": {"available": False, "nodes": []},
                "ocr": None,
                "interactables": [],
                "screenshot_path": screenshot["path"],
                "screenshot_url": screenshot["url"],
                **_scrub_disclosure(screenshot),
                "console_messages": session.console_messages[-10:],
                "page_errors": session.page_errors[-10:],
                "request_failures": session.request_failures[-10:],
                "tabs": tabs,
                "recent_downloads": session.downloads[-10:],
                "takeover_url": self.manager._current_takeover_url(session),
                "remote_access": self.manager.remote_access.session_info(session),
                "preset": "fast",
            }

        if preset == "text":
            interactables = await self.manager.element_ids.evaluate(session.page, INTERACTABLES_SCRIPT, limit)
            summary = await self.page_summary(session.page, text_limit=2000)
            tabs = await self.manager.tabs.summaries(session)
            return {
                "session": await self.manager._session_summary(session),
                "url": session.page.url,
                "title": summary["title"],
                "active_element": summary["active_element"],
                "text_excerpt": summary["text_excerpt"],
                "dom_outline": summary["dom_outline"],
                "accessibility_outline": summary["accessibility_outline"],
                "ocr": None,
                "interactables": interactables,
                "screenshot_path": None,
                "screenshot_url": None,
                "console_messages": session.console_messages[-10:],
                "page_errors": session.page_errors[-10:],
                "request_failures": session.request_failures[-10:],
                "tabs": tabs,
                "recent_downloads": session.downloads[-10:],
                "takeover_url": self.manager._current_takeover_url(session),
                "remote_access": self.manager.remote_access.session_info(session),
                "preset": "text",
            }

        effective_limit = min(limit * 2, 200) if preset == "rich" else limit
        text_limit = 4000 if preset == "rich" else 2000

        async def read_dom() -> tuple[list[dict[str, Any]], dict[str, Any]]:
            # Interactables first: they issue the element ids the summary's
            # active element reports.
            found = await self.manager.element_ids.evaluate(session.page, INTERACTABLES_SCRIPT, effective_limit)
            return found, await self.page_summary(session.page, text_limit=text_limit)

        screenshot, (interactables, summary) = await _gather_settled(
            self.manager._capture_screenshot(session, screenshot_label),
            read_dom(),
        )
        ocr = await self._extract_ocr_if_needed(session, screenshot, summary)
        await self._scrub_screenshot_if_needed(session, screenshot, ocr)
        tabs = await self.manager.tabs.summaries(session)
        return {
            "session": await self.manager._session_summary(session),
            "url": session.page.url,
            "title": summary["title"],
            "active_element": summary["active_element"],
            "text_excerpt": summary["text_excerpt"],
            "dom_outline": summary["dom_outline"],
            "accessibility_outline": summary["accessibility_outline"],
            "ocr": ocr,
            "interactables": interactables,
            "screenshot_path": screenshot["path"],
            "screenshot_url": screenshot["url"],
            **_scrub_disclosure(screenshot),
            "console_messages": session.console_messages[-10:],
            "page_errors": session.page_errors[-10:],
            "request_failures": session.request_failures[-10:],
            "tabs": tabs,
            "recent_downloads": session.downloads[-10:],
            "takeover_url": self.manager._current_takeover_url(session),
            "remote_access": self.manager.remote_access.session_info(session),
            "preset": preset,
        }

    async def _extract_ocr_if_needed(
        self,
        session: "BrowserSession",
        screenshot: dict[str, str],
        summary: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Skip OCR when text extraction already produced usable content.

        Never skips while screenshot PII scrubbing is active — scrubbing
        consumes OCR's bounding boxes, so trading tokens for that would
        weaken PII redaction.
        """
        skip_enabled = self.manager.settings.ocr_skip_when_text_available
        scrubbing_active = self.manager.pii_scrubber.screenshot_enabled
        text_available = bool(summary.get("text_excerpt")) and bool(
            summary.get("accessibility_outline", {}).get("available")
        )
        if skip_enabled and not scrubbing_active and text_available:
            return None
        return await self.manager.ocr.extract_from_image(screenshot["path"])

    async def _capture_screenshot_redacted(self, session: "BrowserSession", label: str) -> dict[str, str]:
        """Capture a screenshot and redact PII pixels before it settles on disk.

        The normal/rich observe path runs OCR for its own payload and redacts via
        `_scrub_screenshot_if_needed`. Every *other* screenshot write — the `fast`
        preset, manual captures, and the before/after snapshot taken on every
        action — skipped redaction entirely, so `PII_SCRUB_SCREENSHOT=true` only
        ever covered a fraction of the images written under /data/artifacts and
        served over /artifacts/.

        OCR here costs time, and it is on by default (PII_SCRUB_SCREENSHOT):
        honouring that setting for some screenshots and not others is the worse
        trade. The before/after snapshots of actions can opt out separately with
        PII_SCRUB_ACTION_SCREENSHOTS=false; see light_snapshot.
        """
        screenshot = await self.manager._capture_screenshot(session, label)
        if self.manager.pii_scrubber.screenshot_enabled:
            ocr = await self.manager.ocr.extract_from_image(screenshot["path"])
            await self._scrub_screenshot_if_needed(session, screenshot, ocr)
        return screenshot

    async def light_snapshot(self, session: "BrowserSession", *, label: str) -> dict[str, Any]:
        self.manager._assert_page_allowed(session)
        # The before/after snapshot of every action. Redacting it runs OCR twice
        # per action, the largest cost an action has; PII_SCRUB_ACTION_SCREENSHOTS
        # =false trades that redaction for speed and leaves observations redacted.
        if self.manager.settings.pii_scrub_action_screenshots:
            capture = self._capture_screenshot_redacted(session, label)
        else:
            capture = self.manager._capture_screenshot(session, label)
        screenshot, summary = await _gather_settled(capture, self.page_summary(session.page))
        return {
            "url": session.page.url,
            "title": summary["title"],
            "active_element": summary["active_element"],
            "text_excerpt": summary["text_excerpt"],
            "dom_outline": summary["dom_outline"],
            "accessibility_outline": summary["accessibility_outline"],
            "screenshot_path": screenshot["path"],
            "screenshot_url": screenshot["url"],
            **_scrub_disclosure(screenshot),
        }

    async def capture_session_screenshot(self, session: "BrowserSession", label: str) -> dict[str, str]:
        # Every screenshot (observe, capture, diff, the action snapshots) comes through here.
        self.manager._assert_page_allowed(session)
        return await self.manager.artifacts.capture_screenshot(session, label)

    def trace_payload(self, session: "BrowserSession") -> dict[str, Any]:
        return self.manager.artifacts.trace_payload(session)

    async def stop_trace_recording(self, session: "BrowserSession") -> None:
        if not self.manager.settings.enable_tracing or not session.trace_recording:
            session.trace_recording = False
            return
        try:
            await session.context.tracing.stop(path=str(session.trace_path))
            session.trace_recording = False
        except Exception as exc:  # pragma: no cover - depends on external browser support
            logger.warning("failed to stop tracing for session %s: %s", session.id, exc)

    async def page_summary(self, page: "Page", text_limit: int = 2000) -> dict[str, Any]:
        # One evaluate returns the text, outline, title and focused element, and
        # it runs alongside the ARIA snapshot rather than after it: this is paid
        # before and after every action, and each round trip costs more when the
        # browser is a remote node. It runs in the controller's isolated world,
        # which holds the focused element's id.
        summary, accessibility_outline = await asyncio.gather(
            self.manager.element_ids.evaluate(page, PAGE_SUMMARY_SCRIPT, text_limit),
            self.accessibility_outline(page),
        )
        return {
            "title": summary.get("title", ""),
            "active_element": summary.get("active_element"),
            "text_excerpt": summary.get("text_excerpt", ""),
            "dom_outline": summary.get("dom_outline", {}),
            "accessibility_outline": accessibility_outline,
        }

    async def accessibility_outline(self, page: "Page") -> dict[str, Any]:
        try:
            snapshot = await page.aria_snapshot(mode="ai", timeout=ARIA_SNAPSHOT_TIMEOUT_MS)
        except Exception as exc:
            logger.debug("failed to capture accessibility snapshot: %s", exc)
            return unavailable_outline("accessibility_snapshot_unavailable")
        # A large page's snapshot runs to megabytes (3 MB, ~0.25 s to parse, for
        # 20,000 links), so it is parsed off the event loop other sessions share.
        return await asyncio.to_thread(outline_from_aria_snapshot, snapshot or "")

    @staticmethod
    def _withhold_screenshot(screenshot: dict[str, str], reason: str) -> NoReturn:
        """PII_SCRUB_SCREENSHOT_STRICT: delete the unscrubbed image and fail the call."""
        Path(screenshot["path"]).unlink(missing_ok=True)
        raise BrowserActionError(
            f"Screenshot withheld: PII_SCRUB_SCREENSHOT_STRICT is on and the screenshot could not be "
            f"PII-scrubbed ({reason}). Run the controller from the Docker image, which ships tesseract, "
            "or set PII_SCRUB_SCREENSHOT_STRICT=false.",
            code=SCREENSHOT_WITHHELD,
            status_code=503,
            retryable=False,
        )

    async def _scrub_screenshot_if_needed(
        self,
        session: "BrowserSession",
        screenshot: dict[str, str],
        ocr: dict[str, Any] | None,
    ) -> None:
        pii_scrubber = self.manager.pii_scrubber
        if not pii_scrubber.screenshot_enabled or not ocr:
            return
        if ocr.get("available", True) is False:
            # No OCR boxes means nothing to redact, and the image goes out as
            # captured. Say so in the result, and in the log once per reason:
            # outside Docker this is every screenshot.
            reason = ocr.get("error") or ("ocr_disabled" if ocr.get("enabled") is False else "ocr_unavailable")
            if getattr(pii_scrubber, "screenshot_strict", False) is True:
                self._withhold_screenshot(screenshot, reason)
            screenshot["pii_scrub_skipped"] = reason
            if reason not in self._warned_unscrubbed:
                self._warned_unscrubbed.add(reason)
                logger.warning(
                    "screenshot PII scrubbing is on but OCR is unavailable (%s): screenshots are returned "
                    "unscrubbed. Run the controller from the Docker image, which ships tesseract.",
                    reason,
                )
            return
        # Prefer the uncapped redaction list. `blocks` is truncated to
        # ocr_max_blocks (default 20) for the model-facing payload, and since
        # image_to_data emits one block per *word*, redacting from it covered
        # only the first ~20 words of a page.
        blocks = ocr.get("redaction_blocks") or ocr.get("blocks")
        if not blocks:
            return
        try:
            scrubbed_path = Path(screenshot["path"])
            raw_bytes = scrubbed_path.read_bytes()
            # PIL decode/draw/encode is CPU-bound and this runs while holding
            # session.lock; keep it off the event loop.
            scrubbed_bytes, hits = await asyncio.to_thread(pii_scrubber.screenshot, raw_bytes, blocks)
            if hits:
                scrubbed_path.write_bytes(scrubbed_bytes)
        except Exception as exc:
            # The file on disk is still the unredacted capture. Say so in the
            # result, the way the OCR-unavailable path does, and audit a failure,
            # not a redaction.
            logger.warning("screenshot PII redaction error for session %s: %s", session.id, exc)
            screenshot["pii_scrub_skipped"] = "redaction_failed"
            await self._audit_screenshot_scrub(
                session, "failed", {"event": "pii_redaction", "layer": "screenshot", "reason": "redaction_failed"}
            )
            if getattr(pii_scrubber, "screenshot_strict", False) is True:
                self._withhold_screenshot(screenshot, "redaction_failed")
            return
        if hits:
            await self._audit_screenshot_scrub(
                session, "ok", pii_scrubber.build_audit_report(session.id, "screenshot", hits)
            )

    async def _audit_screenshot_scrub(self, session: "BrowserSession", status: str, details: dict[str, Any]) -> None:
        if not self.manager.pii_scrubber.audit_report:
            return
        try:
            await self.manager.audit.append(
                event_type="pii_redaction",
                status=status,
                action="screenshot_scrub",
                session_id=session.id,
                details=details,
            )
        except Exception as exc:
            logger.warning("screenshot PII audit error for session %s: %s", session.id, exc)
