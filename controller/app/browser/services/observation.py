from __future__ import annotations

import asyncio
import base64
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ... import events as _events
from ...browser_scripts import ACTIVE_ELEMENT_SCRIPT, INTERACTABLES_SCRIPT, PAGE_SUMMARY_SCRIPT

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...browser_manager import BrowserSession

logger = logging.getLogger(__name__)

ACCESSIBILITY_NODE_LIMIT = 30



# Strict shapes only -- a match is a credential, never ordinary page text.
# Google has two: the legacy standard key ("AIza" + 35) and, since 2026-05-28 the ONLY kind
# Google AI Studio creates, the auth key ("AQ." + a long [A-Za-z0-9._-] body, never ending
# in a dot). Missing the second one is why Emad's freshly created key was not found.
GOOGLE_STANDARD_KEY = r"AIza[0-9A-Za-z_\-]{35}"
GOOGLE_AUTH_KEY = (
    r"(?<![A-Za-z0-9_.\-])AQ\.[A-Za-z0-9_\-][A-Za-z0-9_.\-]{29,509}[A-Za-z0-9_\-](?![A-Za-z0-9_\-])"
)
API_KEY_PATTERNS: dict[str, str] = {
    "google": f"(?:{GOOGLE_STANDARD_KEY})|(?:{GOOGLE_AUTH_KEY})",
}
_API_KEY_MARKERS = ("AIza", "AQ.")

# The clipboard is read only on the provider's own pages (the "Copy" button of its
# "API key created" dialog), never on an arbitrary site. Hosts, exact or as a parent domain.
CLIPBOARD_KEY_HOSTS: dict[str, tuple[str, ...]] = {
    "google": ("aistudio.google.com", "console.cloud.google.com", "makersuite.google.com"),
}
MAX_KEY_FRAMES = 25

_API_KEY_RE = re.compile("|".join(f"(?:{pattern})" for pattern in API_KEY_PATTERNS.values()))
REDACTED_API_KEY = "[api key hidden]"


def redact_api_keys(value: Any) -> Any:
    """Every observation/snapshot passes through this before it leaves the controller or is
    written to actions.jsonl / audit: a key shown on the page ("API key created" dialogs)
    must never reach the calling agent's model or our logs. find_api_keys is the one,
    pattern-limited way to read it."""
    if isinstance(value, str):
        if any(marker in value for marker in _API_KEY_MARKERS):
            return _API_KEY_RE.sub(REDACTED_API_KEY, value)
        return value
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

# Reads the clipboard in the page and hands back ONLY key-shaped matches; the clipboard is
# emptied only when it held one (an unrelated clipboard the owner copied is left alone).
CLIPBOARD_API_KEYS_SCRIPT = """
async (pattern) => {
  let text = '';
  try { text = await navigator.clipboard.readText(); } catch (e) { return {keys: [], error: true}; }
  const keys = typeof text === 'string' ? Array.from(text.matchAll(new RegExp(pattern, 'g')), m => m[0]) : [];
  if (keys.length) { try { await navigator.clipboard.writeText(''); } catch (e) {} }
  return {keys: keys.slice(0, 5), error: false};
}
"""


def _host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"https", "http"} or not host:
        return False
    return any(host == allowed or host.endswith("." + allowed) for allowed in hosts)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


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
        """API keys of ONE known shape shown on the page -- or just copied by its "Copy" button.

        The narrowest read that lets the agent's server store a key the owner's employee just
        created (e.g. Google AI Studio's "API key created" dialog) without the key ever going
        through the model: only strings matching the provider's strict key pattern come back,
        nothing else from the page. observe() keeps redacting them (see API_KEY_PATTERNS).

        Looks in every frame of the tab (a dialog may live in an iframe); when none shows a
        key and the tab is on the provider's own site, reads the clipboard (a dialog that
        shows the key masked still copies the full key) -- matches only, cleared after.
        Through X-Tab-Id this is the employee's own tab, never the owner's active one."""
        pattern = API_KEY_PATTERNS.get(provider)
        if pattern is None:
            raise ValueError("unknown provider")
        session = await self.manager.get_session(session_id)
        async with session.lock:
            found, source = await self.manager.session_lifecycle.guarded(
                session,
                self._collect_api_keys(session.page, provider, pattern),
                what="find_api_keys",
                timeout=self.manager.settings.browser_call_timeout_seconds,
            )
        strict = re.compile(pattern)
        keys = [key for key in (found or []) if isinstance(key, str) and strict.fullmatch(key)]
        keys = list(dict.fromkeys(keys))[:5]
        return {"provider": provider, "keys": keys, "source": source if keys else None, "url": session.page.url}

    async def _collect_api_keys(self, page: Any, provider: str, pattern: str) -> tuple[list[Any], str | None]:
        found: list[Any] = []
        frames = list(getattr(page, "frames", None) or [])[:MAX_KEY_FRAMES]
        if not frames:
            found.extend(await page.evaluate(FIND_API_KEYS_SCRIPT, pattern) or [])
        for frame in frames:
            try:
                found.extend(await frame.evaluate(FIND_API_KEYS_SCRIPT, pattern) or [])
            except Exception as exc:  # a detached / navigating frame: skip it, keep the rest
                logger.debug("find_api_keys: frame skipped: %s", type(exc).__name__)
        if found:
            return found, "page"
        url = getattr(page, "url", "") or ""
        if not _host_allowed(url, CLIPBOARD_KEY_HOSTS.get(provider, ())):
            return [], None
        return await self._clipboard_api_keys(page, url, pattern), "clipboard"

    async def _clipboard_api_keys(self, page: Any, url: str, pattern: str) -> list[Any]:
        context = page.context
        try:
            await context.grant_permissions(["clipboard-read", "clipboard-write"], origin=_origin(url))
        except Exception as exc:
            logger.warning("find_api_keys: clipboard permission not granted: %s", type(exc).__name__)
            return []
        try:
            result = await page.evaluate(CLIPBOARD_API_KEYS_SCRIPT, pattern)
        except Exception as exc:
            logger.warning("find_api_keys: clipboard read failed: %s", type(exc).__name__)
            return []
        finally:
            # Nothing else in the controller grants permissions: drop the override again so
            # the site does not keep clipboard access.
            try:
                await context.clear_permissions()
            except Exception:
                pass
        if not isinstance(result, dict):
            return []
        return list(result.get("keys") or [])

    async def capture_screenshot(self, session_id: str, *, label: str = "manual") -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            screenshot = await self.manager.session_lifecycle.guarded(
                session,
                self._capture_screenshot_redacted(session, label),
                what="screenshot",
                timeout=self.manager.settings.browser_action_timeout_seconds,
            )
            # Read back the file AFTER redaction (PII scrubbing rewrites the same path in
            # place -- see _capture_screenshot_redacted) so an agent asking for these bytes
            # (browser_see, social-operator) never receives an unscrubbed frame. Bounded to
            # this one on-demand capture, never the ordinary observe() path, so routine
            # look/click/type calls pay no extra encode cost.
            return {
                "session": await self.manager._session_summary(session),
                "url": session.page.url,
                "screenshot_path": screenshot["path"],
                "screenshot_url": screenshot["url"],
                "screenshot_base64": await self._read_screenshot_base64(screenshot["path"]),
                "takeover_url": self.manager._current_takeover_url(session),
            }

    @staticmethod
    async def _read_screenshot_base64(path: str) -> str | None:
        try:
            data = await asyncio.to_thread(Path(path).read_bytes)
        except OSError:
            return None
        return base64.b64encode(data).decode("ascii")

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
