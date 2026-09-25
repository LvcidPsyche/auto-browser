from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ... import events as _events
from ...browser_scripts import ACTIVE_ELEMENT_SCRIPT, INTERACTABLES_SCRIPT, PAGE_SUMMARY_SCRIPT

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...browser_manager import BrowserSession

logger = logging.getLogger(__name__)

ACCESSIBILITY_NODE_LIMIT = 30



# Strict shapes only -- a match is a credential, never ordinary page text.
API_KEY_PATTERNS: dict[str, str] = {
    "google": r"AIza[0-9A-Za-z_\-]{35}",
}

_API_KEY_RE = re.compile("|".join(f"(?:{pattern})" for pattern in API_KEY_PATTERNS.values()))
REDACTED_API_KEY = "[api key hidden]"


def redact_api_keys(value: Any) -> Any:
    """Every observation/snapshot passes through this before it leaves the controller or is
    written to actions.jsonl / audit: a key shown on the page ("API key created" dialogs)
    must never reach the calling agent's model or our logs. find_api_keys is the one,
    pattern-limited way to read it."""
    if isinstance(value, str):
        return _API_KEY_RE.sub(REDACTED_API_KEY, value) if "AIza" in value else value
    if isinstance(value, dict):
        return {key: redact_api_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_api_keys(item) for item in value]
    return value


FIND_API_KEYS_SCRIPT = """
(pattern) => {
  const re = new RegExp(pattern, 'g');
  const found = new Set();
  const scan = (value) => {
    if (typeof value !== 'string' || value.length < 20) return;
    for (const match of value.matchAll(re)) found.add(match[0]);
  };
  const visit = (root) => {
    scan(root.body ? root.body.innerText : root.textContent);
    for (const field of root.querySelectorAll('input, textarea')) scan(field.value);
    for (const el of root.querySelectorAll('[value], [data-value], [aria-label], [title]')) {
      scan(el.getAttribute('value')); scan(el.getAttribute('data-value'));
      scan(el.getAttribute('aria-label')); scan(el.getAttribute('title'));
    }
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);
  };
  visit(document);
  return Array.from(found).slice(0, 5);
}
"""

class BrowserObservationService:
    """Encapsulates observation, screenshot, and trace payload helpers."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def observe(self, session_id: str, limit: int = 40, preset: str | None = None) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            result = await self.manager.session_lifecycle.guarded(
                session,
                self.observation_payload(session, limit=limit, preset=preset),
                what="observe",
                timeout=self.manager.settings.browser_call_timeout_seconds,
            )
            result = redact_api_keys(result)
            _events.emit_observe(
                session_id,
                result.get("url", ""),
                result.get("title", ""),
                result.get("screenshot_url"),
            )
            return result

    async def find_api_keys(self, session_id: str, provider: str) -> dict[str, Any]:
        """API keys of ONE known shape shown on the page (text or a field's value).

        The narrowest read that lets the agent's server store a key the owner's employee just
        created (e.g. Google AI Studio's "API key created" dialog) without the key ever going
        through the model: only strings matching the provider's strict key pattern come back,
        nothing else from the page. observe() keeps redacting them (see API_KEY_PATTERNS)."""
        pattern = API_KEY_PATTERNS.get(provider)
        if pattern is None:
            raise ValueError("unknown provider")
        session = await self.manager.get_session(session_id)
        async with session.lock:
            found = await self.manager.session_lifecycle.guarded(
                session,
                session.page.evaluate(FIND_API_KEYS_SCRIPT, pattern),
                what="find_api_keys",
                timeout=self.manager.settings.browser_call_timeout_seconds,
            )
        keys = [key for key in (found or []) if isinstance(key, str)]
        return {"provider": provider, "keys": list(dict.fromkeys(keys))[:5], "url": session.page.url}

    async def capture_screenshot(self, session_id: str, *, label: str = "manual") -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            screenshot = await self.manager.session_lifecycle.guarded(
                session,
                self._capture_screenshot_redacted(session, label),
                what="screenshot",
                timeout=self.manager.settings.browser_action_timeout_seconds,
            )
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
        preset: str | None = None,
    ) -> dict[str, Any]:
        from .dialogs import BrowserDialogService

        dialogs = getattr(self.manager, "dialogs", None)
        if not isinstance(dialogs, BrowserDialogService):
            return await self._observation_payload(
                session, limit=limit, screenshot_label=screenshot_label, preset=preset
            )
        dialogs.heal_active_page(session)
        open_dialog = await dialogs.open_dialog(session)
        if open_dialog is not None:
            # The tab is blocked by a JavaScript dialog: every page read would
            # hang until it is answered, so report the dialog itself instead.
            return {
                "session": await self.manager._session_summary(session),
                "url": session.page.url,
                "title": "",
                "active_element": None,
                "text_excerpt": f"[{open_dialog.get('type')} dialog] {open_dialog.get('message', '')}",
                "dom_outline": {},
                "accessibility_outline": {"available": False, "nodes": []},
                "ocr": None,
                "interactables": [],
                "screenshot_path": None,
                "screenshot_url": None,
                "console_messages": session.console_messages[-10:],
                "page_errors": session.page_errors[-10:],
                "request_failures": session.request_failures[-10:],
                "tabs": [],
                "recent_downloads": session.downloads[-10:],
                "takeover_url": self.manager._current_takeover_url(session),
                "remote_access": self.manager.remote_access.session_info(session),
                "preset": preset or self.manager.settings.perception_preset_default,
                "open_dialog": open_dialog,
                "recent_dialogs": session.dialog_log[-5:],
            }
        payload = await self._observation_payload(
            session, limit=limit, screenshot_label=screenshot_label, preset=preset
        )
        payload["open_dialog"] = None
        payload["recent_dialogs"] = session.dialog_log[-5:]
        return payload

    async def _observation_payload(
        self,
        session: "BrowserSession",
        *,
        limit: int = 40,
        screenshot_label: str = "observe",
        preset: str | None = None,
    ) -> dict[str, Any]:
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
            interactables = await session.page.evaluate(INTERACTABLES_SCRIPT, limit)
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

        screenshot = await self.manager._capture_screenshot(session, screenshot_label)
        effective_limit = min(limit * 2, 200) if preset == "rich" else limit
        interactables = await session.page.evaluate(INTERACTABLES_SCRIPT, effective_limit)
        text_limit = 4000 if preset == "rich" else 2000
        summary = await self.page_summary(session.page, text_limit=text_limit)
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

        OCR here costs time, but it is gated on the operator having explicitly
        asked for screenshot scrubbing; silently honouring that setting for some
        screenshots and not others is the worse trade.
        """
        screenshot = await self.manager._capture_screenshot(session, label)
        if self.manager.pii_scrubber.screenshot_enabled:
            ocr = await self.manager.ocr.extract_from_image(screenshot["path"])
            await self._scrub_screenshot_if_needed(session, screenshot, ocr)
        return screenshot

    async def light_snapshot(self, session: "BrowserSession", *, label: str) -> dict[str, Any]:
        screenshot = await self._capture_screenshot_redacted(session, label)
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
        if not pii_scrubber.screenshot_enabled or not ocr:
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
