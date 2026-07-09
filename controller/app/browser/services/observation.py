from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ... import events as _events
from ...browser_scripts import ACTIVE_ELEMENT_SCRIPT, INTERACTABLES_SCRIPT, PAGE_SUMMARY_SCRIPT

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...browser_manager import BrowserSession

logger = logging.getLogger(__name__)

ACCESSIBILITY_NODE_LIMIT = 30


class BrowserObservationService:
    """Encapsulates observation, screenshot, and trace payload helpers."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def observe(self, session_id: str, limit: int = 40, preset: str = "normal") -> dict[str, Any]:
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

    async def capture_screenshot(self, session_id: str, *, label: str = "manual") -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            screenshot = await self.manager._capture_screenshot(session, label)
            return {
                "session": await self.manager._session_summary(session),
                "url": session.page.url,
                "screenshot_path": screenshot["path"],
                "screenshot_url": screenshot["url"],
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
        preset: str = "normal",
    ) -> dict[str, Any]:
        screenshot = await self.manager._capture_screenshot(session, screenshot_label)

        if preset == "fast":
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
                "console_messages": session.console_messages[-10:],
                "page_errors": session.page_errors[-10:],
                "request_failures": [],
                "tabs": tabs,
                "recent_downloads": session.downloads[-10:],
                "takeover_url": self.manager._current_takeover_url(session),
                "remote_access": self.manager.remote_access.session_info(session),
                "preset": "fast",
            }

        effective_limit = min(limit * 2, 200) if preset == "rich" else limit
        interactables = await session.page.evaluate(INTERACTABLES_SCRIPT, effective_limit)
        text_limit = 4000 if preset == "rich" else 2000
        summary = await self.page_summary(session.page, text_limit=text_limit)
        ocr = await self.manager.ocr.extract_from_image(screenshot["path"])
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
            "console_messages": session.console_messages[-10:],
            "page_errors": session.page_errors[-10:],
            "request_failures": session.request_failures[-10:],
            "tabs": tabs,
            "recent_downloads": session.downloads[-10:],
            "takeover_url": self.manager._current_takeover_url(session),
            "remote_access": self.manager.remote_access.session_info(session),
            "preset": preset,
        }

    async def light_snapshot(self, session: "BrowserSession", *, label: str) -> dict[str, Any]:
        screenshot = await self.manager._capture_screenshot(session, label)
        summary = await self.page_summary(session.page)
        return {
            "url": session.page.url,
            "title": summary["title"],
            "active_element": summary["active_element"],
            "text_excerpt": summary["text_excerpt"],
            "dom_outline": summary["dom_outline"],
            "accessibility_outline": summary["accessibility_outline"],
            "screenshot_path": screenshot["path"],
            "screenshot_url": screenshot["url"],
        }

    async def capture_session_screenshot(self, session: "BrowserSession", label: str) -> dict[str, str]:
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
        summary = await page.evaluate(PAGE_SUMMARY_SCRIPT, text_limit)
        accessibility_outline = await self.accessibility_outline(page)
        return {
            "title": await page.title(),
            "active_element": await page.evaluate(ACTIVE_ELEMENT_SCRIPT),
            "text_excerpt": summary.get("text_excerpt", ""),
            "dom_outline": summary.get("dom_outline", {}),
            "accessibility_outline": accessibility_outline,
        }

    async def accessibility_outline(self, page: "Page") -> dict[str, Any]:
        accessibility = getattr(page, "accessibility", None)
        if accessibility is None or not hasattr(accessibility, "snapshot"):
            return {
                "available": False,
                "root_role": None,
                "root_name": None,
                "focused": None,
                "role_counts": {},
                "nodes": [],
            }

        try:
            snapshot = await accessibility.snapshot(interesting_only=True)
        except Exception as exc:
            logger.debug("failed to capture accessibility snapshot: %s", exc)
            return {
                "available": False,
                "root_role": None,
                "root_name": None,
                "focused": None,
                "role_counts": {},
                "nodes": [],
                "error": "accessibility_snapshot_unavailable",
            }

        if not snapshot:
            return {
                "available": True,
                "root_role": None,
                "root_name": None,
                "focused": None,
                "role_counts": {},
                "nodes": [],
            }

        nodes: list[dict[str, Any]] = []
        role_counts: dict[str, int] = {}
        focused: dict[str, Any] | None = None

        def walk(node: dict[str, Any], depth: int) -> None:
            nonlocal focused
            if len(nodes) >= ACCESSIBILITY_NODE_LIMIT:
                return
            role = node.get("role")
            if isinstance(role, str) and role:
                role_counts[role] = role_counts.get(role, 0) + 1
            compact = {
                "role": role,
                "name": node.get("name"),
                "value": node.get("valueString") or node.get("value"),
                "description": node.get("description"),
                "focused": bool(node.get("focused")),
                "disabled": bool(node.get("disabled")),
                "selected": bool(node.get("selected")),
                "checked": node.get("checked"),
                "expanded": node.get("expanded"),
                "pressed": node.get("pressed"),
                "depth": depth,
            }
            nodes.append(compact)
            if compact["focused"] and focused is None:
                focused = compact
            for child in node.get("children") or []:
                if not isinstance(child, dict):
                    continue
                walk(child, depth + 1)
                if len(nodes) >= ACCESSIBILITY_NODE_LIMIT:
                    return

        walk(snapshot, 0)
        return {
            "available": True,
            "root_role": snapshot.get("role"),
            "root_name": snapshot.get("name"),
            "focused": focused,
            "role_counts": role_counts,
            "nodes": nodes,
        }

    async def _scrub_screenshot_if_needed(
        self,
        session: "BrowserSession",
        screenshot: dict[str, str],
        ocr: dict[str, Any] | None,
    ) -> None:
        pii_scrubber = self.manager.pii_scrubber
        if not pii_scrubber.screenshot_enabled or not ocr or not ocr.get("blocks"):
            return
        try:
            scrubbed_path = Path(screenshot["path"])
            raw_bytes = scrubbed_path.read_bytes()
            scrubbed_bytes, hits = pii_scrubber.screenshot(raw_bytes, ocr["blocks"])
            if hits:
                scrubbed_path.write_bytes(scrubbed_bytes)
                if pii_scrubber.audit_report:
                    await self.manager.audit.append(
                        event_type="pii_redaction",
                        status="ok",
                        action="screenshot_scrub",
                        session_id=session.id,
                        details=pii_scrubber.build_audit_report(session.id, "screenshot", hits),
                    )
        except Exception as exc:
            logger.warning("screenshot PII redaction error for session %s: %s", session.id, exc)
