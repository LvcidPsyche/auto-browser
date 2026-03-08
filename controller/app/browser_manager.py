from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from urllib.request import urlopen
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Page, Playwright, async_playwright

from .config import Settings

logger = logging.getLogger(__name__)

INTERACTABLES_SCRIPT = r"""
(limit) => {
  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }

  function getLabel(el) {
    const raw = el.getAttribute('aria-label')
      || el.getAttribute('placeholder')
      || el.innerText
      || el.value
      || el.getAttribute('name')
      || el.id
      || el.href
      || '';
    return String(raw).replace(/\s+/g, ' ').trim().slice(0, 160);
  }

  const selector = [
    'a',
    'button',
    'input',
    'textarea',
    'select',
    '[role="button"]',
    '[role="link"]',
    '[role="textbox"]',
    '[contenteditable="true"]',
    '[tabindex]'
  ].join(',');

  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    if (!isVisible(el) || el.closest('[aria-hidden="true"]')) continue;
    if (!el.dataset.operatorId) {
      el.dataset.operatorId = `op-${Math.random().toString(36).slice(2, 10)}`;
    }
    const rect = el.getBoundingClientRect();
    out.push({
      element_id: el.dataset.operatorId,
      selector_hint: `[data-operator-id="${el.dataset.operatorId}"]`,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type'),
      role: el.getAttribute('role') || el.tagName.toLowerCase(),
      label: getLabel(el),
      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      href: el.href || null,
      bbox: {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      }
    });
    if (out.length >= limit) break;
  }
  return out;
}
"""

ACTIVE_ELEMENT_SCRIPT = r"""
() => {
  const el = document.activeElement;
  if (!el) return null;
  return {
    tag: el.tagName.toLowerCase(),
    element_id: el.dataset?.operatorId || null,
    name: el.getAttribute('name'),
    id: el.id || null,
    label: (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.innerText || el.value || '').toString().replace(/\s+/g, ' ').trim().slice(0, 120)
  };
}
"""


@dataclass
class BrowserSession:
    id: str
    name: str
    created_at: datetime
    context: BrowserContext
    page: Page
    artifact_dir: Path
    takeover_url: str
    trace_path: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    console_messages: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    request_failures: list[dict[str, Any]] = field(default_factory=list)
    last_action: str | None = None


class BrowserManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.sessions: dict[str, BrowserSession] = {}
        self._browser_lock = asyncio.Lock()

        Path(self.settings.artifact_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.upload_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.auth_root).mkdir(parents=True, exist_ok=True)

    async def startup(self) -> None:
        logger.info("starting browser manager")
        self.playwright = await async_playwright().start()
        await self.ensure_browser()

    async def shutdown(self) -> None:
        logger.info("shutting down browser manager")
        session_ids = list(self.sessions.keys())
        for session_id in session_ids:
            try:
                await self.close_session(session_id)
            except Exception as exc:  # pragma: no cover - best effort cleanup
                logger.warning("failed to close session %s during shutdown: %s", session_id, exc)

        if self.browser is not None and self.browser.is_connected():
            await self.browser.close()
        if self.playwright is not None:
            await self.playwright.stop()

    async def ensure_browser(self) -> Browser:
        async with self._browser_lock:
            if self.browser is not None and self.browser.is_connected():
                return self.browser
            if self.playwright is None:
                raise RuntimeError("Playwright not started")

            last_error: Exception | None = None
            for attempt in range(1, self.settings.connect_retries + 1):
                try:
                    cdp_target = await self._resolve_cdp_target(self.settings.browser_cdp_endpoint)
                    self.browser = await self.playwright.chromium.connect_over_cdp(cdp_target)
                    logger.info("connected to browser node on attempt %s via %s", attempt, cdp_target)
                    return self.browser
                except Exception as exc:  # pragma: no cover - depends on external service
                    last_error = exc
                    await asyncio.sleep(self.settings.connect_retry_delay_seconds)

            raise RuntimeError(
                f"Unable to connect to browser node at {self.settings.browser_cdp_endpoint}"
            ) from last_error

    async def _resolve_cdp_target(self, endpoint: str) -> str:
        ws_endpoint_file = Path(self.settings.browser_cdp_ws_endpoint_file)
        if ws_endpoint_file.exists():
            ws_endpoint = ws_endpoint_file.read_text(encoding="utf-8").strip()
            if ws_endpoint:
                return ws_endpoint

        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"}:
            return endpoint

        version_url = f"{endpoint.rstrip('/')}/json/version"

        def fetch_version() -> dict[str, Any]:
            with urlopen(version_url, timeout=5) as response:
                return json.load(response)

        version_info = await asyncio.to_thread(fetch_version)
        ws_url = version_info.get("webSocketDebuggerUrl")
        if not ws_url:
            return endpoint

        ws_parsed = urlparse(ws_url)
        if ws_parsed.hostname not in {"127.0.0.1", "0.0.0.0", "localhost"}:
            return ws_url

        host = parsed.hostname or ws_parsed.hostname or "127.0.0.1"
        port = parsed.port or ws_parsed.port
        netloc = f"{host}:{port}" if port else host
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunparse(
            (
                scheme,
                netloc,
                ws_parsed.path,
                ws_parsed.params,
                ws_parsed.query,
                ws_parsed.fragment,
            )
        )

    async def list_sessions(self) -> list[dict[str, Any]]:
        return [await self._session_summary(session) for session in self.sessions.values()]

    async def create_session(
        self,
        *,
        name: str | None = None,
        start_url: str | None = None,
        storage_state_path: str | None = None,
    ) -> dict[str, Any]:
        if start_url:
            self._assert_url_allowed(start_url)
        if len(self.sessions) >= self.settings.max_sessions:
            raise RuntimeError(
                f"POC limit reached: max_sessions={self.settings.max_sessions}. "
                "This scaffold uses one visible desktop, so keep one active session per browser node."
            )

        browser = await self.ensure_browser()
        session_id = uuid4().hex[:12]
        artifact_dir = Path(self.settings.artifact_root) / session_id
        artifact_dir.mkdir(parents=True, exist_ok=True)

        context_kwargs: dict[str, Any] = {
            "viewport": {
                "width": self.settings.default_viewport_width,
                "height": self.settings.default_viewport_height,
            },
            "accept_downloads": True,
        }
        if storage_state_path:
            context_kwargs["storage_state"] = str(self._safe_auth_path(storage_state_path, must_exist=True))

        context: BrowserContext | None = None
        session: BrowserSession | None = None
        try:
            context = await browser.new_context(**context_kwargs)
            if self.settings.enable_tracing:
                await context.tracing.start(screenshots=True, snapshots=True, sources=False)

            page = await context.new_page()
            page.set_default_timeout(self.settings.action_timeout_ms)
            session = BrowserSession(
                id=session_id,
                name=name or f"session-{session_id}",
                created_at=datetime.now(UTC),
                context=context,
                page=page,
                artifact_dir=artifact_dir,
                takeover_url=self.settings.takeover_url,
                trace_path=artifact_dir / "trace.zip",
            )
            self._attach_page_listeners(page, session)
            self.sessions[session_id] = session

            if start_url:
                await page.goto(start_url, wait_until="domcontentloaded")
                await self._settle(page)

            return await self._session_summary(session)
        except Exception:
            self.sessions.pop(session_id, None)
            if context is not None:
                await context.close()
            raise

    async def get_session(self, session_id: str) -> BrowserSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    async def observe(self, session_id: str, limit: int = 40) -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            return await self._observation_payload(session, limit=limit)

    async def navigate(self, session_id: str, url: str) -> dict[str, Any]:
        self._assert_url_allowed(url)
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.goto(url, wait_until="domcontentloaded")
            await self._settle(session.page)

        return await self._run_action(session, "navigate", {"url": url}, operation)

    async def click(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = self._resolve_target(selector=selector, element_id=element_id, x=x, y=y)

        async def operation() -> None:
            if target["mode"] == "coordinates":
                await session.page.mouse.click(float(x), float(y))
            else:
                locator = session.page.locator(target["selector"]).first
                await locator.scroll_into_view_if_needed()
                await locator.click()
            await self._settle(session.page)

        return await self._run_action(session, "click", target, operation)

    async def type(
        self,
        session_id: str,
        *,
        text: str,
        selector: str | None = None,
        element_id: str | None = None,
        clear_first: bool = True,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = self._resolve_target(selector=selector, element_id=element_id)

        async def operation() -> None:
            locator = session.page.locator(target["selector"]).first
            await locator.scroll_into_view_if_needed()
            await locator.click()
            if clear_first:
                try:
                    await locator.fill(text)
                except Exception:
                    await session.page.keyboard.press("Control+A")
                    await session.page.keyboard.type(text, delay=self.settings.typing_delay_ms)
            else:
                await session.page.keyboard.type(text, delay=self.settings.typing_delay_ms)
            await self._settle(session.page)

        return await self._run_action(
            session,
            "type",
            {**target, "clear_first": clear_first, "text_preview": text[:80]},
            operation,
        )

    async def press(self, session_id: str, key: str) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.keyboard.press(key)
            await self._settle(session.page)

        return await self._run_action(session, "press", {"key": key}, operation)

    async def scroll(self, session_id: str, delta_x: float, delta_y: float) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.mouse.wheel(delta_x, delta_y)
            await self._settle(session.page)

        return await self._run_action(
            session,
            "scroll",
            {"delta_x": delta_x, "delta_y": delta_y},
            operation,
        )

    async def upload(
        self,
        session_id: str,
        *,
        file_path: str,
        approved: bool,
        selector: str | None = None,
        element_id: str | None = None,
    ) -> dict[str, Any]:
        if self.settings.require_approval_for_uploads and not approved:
            raise PermissionError("upload actions require approved=true in this POC")

        session = await self.get_session(session_id)
        target = self._resolve_target(selector=selector, element_id=element_id)
        safe_path = self._safe_upload_path(file_path)

        async def operation() -> None:
            locator = session.page.locator(target["selector"]).first
            await locator.set_input_files(str(safe_path))
            await self._settle(session.page)

        return await self._run_action(
            session,
            "upload",
            {**target, "file_path": str(safe_path), "approved": approved},
            operation,
        )

    async def save_storage_state(self, session_id: str, path: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        safe_path = self._safe_auth_path(path)
        async with session.lock:
            await session.context.storage_state(path=str(safe_path))
            payload = {
                "saved_to": str(safe_path),
                "session": await self._session_summary(session),
            }
            await self._append_jsonl(
                session.artifact_dir / "actions.jsonl",
                {"timestamp": self._timestamp(), "action": "save_storage_state", **payload},
            )
            return payload

    async def request_human_takeover(self, session_id: str, reason: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        payload = {
            "session": await self._session_summary(session),
            "reason": reason,
            "takeover_url": session.takeover_url,
            "message": "Human takeover requested. Open the noVNC URL to continue visually. In this POC, takeover is global to the single browser desktop.",
        }
        await self._append_jsonl(
            session.artifact_dir / "actions.jsonl",
            {"timestamp": self._timestamp(), "action": "request_human_takeover", **payload},
        )
        return payload

    async def close_session(self, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            summary = await self._session_summary(session)
            if self.settings.enable_tracing:
                try:
                    await session.context.tracing.stop(path=str(session.trace_path))
                except Exception as exc:  # pragma: no cover - depends on external browser support
                    logger.warning("failed to stop tracing for session %s: %s", session_id, exc)
            await session.context.close()
            self.sessions.pop(session_id, None)
            return {"closed": True, "trace_path": str(session.trace_path), "session": summary}

    async def _run_action(
        self,
        session: BrowserSession,
        action_name: str,
        target: dict[str, Any],
        operation,
    ) -> dict[str, Any]:
        async with session.lock:
            before = await self._light_snapshot(session, label=f"before-{action_name}")
            try:
                await operation()
                self._assert_runtime_url_allowed(session.page.url)
            except PermissionError as exc:
                try:
                    if session.page.url != before.get("url"):
                        await session.page.go_back(wait_until="domcontentloaded")
                        await self._settle(session.page)
                except Exception:
                    pass
                failed = await self._light_snapshot(session, label=f"blocked-{action_name}")
                await self._append_jsonl(
                    session.artifact_dir / "actions.jsonl",
                    {
                        "timestamp": self._timestamp(),
                        "action": action_name,
                        "status": "blocked",
                        "target": target,
                        "before": before,
                        "after": failed,
                        "error": str(exc),
                    },
                )
                raise
            except PlaywrightError as exc:
                failed = await self._light_snapshot(session, label=f"failed-{action_name}")
                await self._append_jsonl(
                    session.artifact_dir / "actions.jsonl",
                    {
                        "timestamp": self._timestamp(),
                        "action": action_name,
                        "status": "failed",
                        "target": target,
                        "before": before,
                        "after": failed,
                        "error": str(exc),
                    },
                )
                raise ValueError(
                    f"Action failed for {action_name}. Refresh observation and retry. Details: {exc}"
                ) from exc
            after = await self._observation_payload(session, limit=20, screenshot_label=f"after-{action_name}")
            session.last_action = action_name
            payload = {
                "timestamp": self._timestamp(),
                "action": action_name,
                "target": target,
                "before": before,
                "after": after,
            }
            await self._append_jsonl(session.artifact_dir / "actions.jsonl", payload)
            return {
                "action": action_name,
                "session": await self._session_summary(session),
                "before": before,
                "after": after,
                "target": target,
            }

    async def _observation_payload(
        self,
        session: BrowserSession,
        *,
        limit: int = 40,
        screenshot_label: str = "observe",
    ) -> dict[str, Any]:
        interactables = await session.page.evaluate(INTERACTABLES_SCRIPT, limit)
        screenshot = await self._capture_screenshot(session, screenshot_label)
        title = await session.page.title()
        active_element = await session.page.evaluate(ACTIVE_ELEMENT_SCRIPT)
        return {
            "session": await self._session_summary(session),
            "url": session.page.url,
            "title": title,
            "active_element": active_element,
            "interactables": interactables,
            "screenshot_path": screenshot["path"],
            "screenshot_url": screenshot["url"],
            "console_messages": session.console_messages[-10:],
            "page_errors": session.page_errors[-10:],
            "request_failures": session.request_failures[-10:],
            "takeover_url": session.takeover_url,
        }

    async def _light_snapshot(self, session: BrowserSession, *, label: str) -> dict[str, Any]:
        screenshot = await self._capture_screenshot(session, label)
        title = await session.page.title()
        active_element = await session.page.evaluate(ACTIVE_ELEMENT_SCRIPT)
        return {
            "url": session.page.url,
            "title": title,
            "active_element": active_element,
            "screenshot_path": screenshot["path"],
            "screenshot_url": screenshot["url"],
        }

    async def _capture_screenshot(self, session: BrowserSession, label: str) -> dict[str, str]:
        filename = f"{self._timestamp()}-{label}.png"
        path = session.artifact_dir / filename
        await session.page.screenshot(path=str(path), full_page=False)
        return {"path": str(path), "url": f"/artifacts/{session.id}/{filename}"}

    async def _session_summary(self, session: BrowserSession) -> dict[str, Any]:
        return {
            "id": session.id,
            "name": session.name,
            "created_at": session.created_at.isoformat(),
            "current_url": session.page.url,
            "title": await session.page.title(),
            "artifact_dir": str(session.artifact_dir),
            "takeover_url": session.takeover_url,
            "last_action": session.last_action,
        }

    async def _settle(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=min(self.settings.action_timeout_ms, 5000))
        except Exception:
            pass
        await page.wait_for_timeout(250)

    def _assert_runtime_url_allowed(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme in {"about", "data", "blob", ""}:
            return
        self._assert_url_allowed(url)

    def _assert_url_allowed(self, url: str) -> None:
        host = urlparse(url).hostname
        if not host:
            raise PermissionError(f"Could not determine hostname for URL: {url}")
        patterns = self.settings.allowed_host_patterns
        if not patterns or patterns == ["*"]:
            return
        for pattern in patterns:
            normalized = pattern.removeprefix("*.")
            if fnmatch.fnmatch(host, pattern) or host == normalized or host.endswith(f".{normalized}"):
                return
        raise PermissionError(f"Host {host!r} is not allowlisted")

    def _resolve_target(
        self,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        if element_id:
            return {
                "mode": "selector",
                "element_id": element_id,
                "selector": f'[data-operator-id="{element_id}"]',
            }
        if selector:
            return {"mode": "selector", "selector": selector}
        if x is not None and y is not None:
            return {"mode": "coordinates", "x": x, "y": y}
        raise ValueError("Provide selector, element_id, or x+y coordinates")

    def _safe_upload_path(self, file_path: str) -> Path:
        root = Path(self.settings.upload_root).resolve()
        candidate = (root / file_path).resolve() if not Path(file_path).is_absolute() else Path(file_path).resolve()
        if root not in candidate.parents and candidate != root:
            raise PermissionError("file_path must stay inside upload root")
        if not candidate.exists():
            raise FileNotFoundError(candidate)
        return candidate

    def _safe_auth_path(self, relative_path: str, must_exist: bool = False) -> Path:
        root = Path(self.settings.auth_root).resolve()
        candidate = (root / relative_path).resolve()
        if root not in candidate.parents and candidate != root:
            raise PermissionError("auth path must stay inside auth root")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if must_exist and not candidate.exists():
            raise FileNotFoundError(candidate)
        return candidate

    def _attach_page_listeners(self, page: Page, session: BrowserSession) -> None:
        page.on("console", lambda message: self._bounded_append(
            session.console_messages,
            {
                "type": message.type,
                "text": message.text,
                "location": message.location,
            },
        ))
        page.on("pageerror", lambda error: self._bounded_append(session.page_errors, str(error)))
        page.on("requestfailed", lambda request: self._bounded_append(
            session.request_failures,
            {
                "url": request.url,
                "method": request.method,
                "failure": str(request.failure) if request.failure else None,
            },
        ))

    def _bounded_append(self, items: list[Any], value: Any, limit: int = 50) -> None:
        items.append(value)
        if len(items) > limit:
            del items[: len(items) - limit]

    async def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False)
        await asyncio.to_thread(self._append_text, path, line + "\n")

    @staticmethod
    def _append_text(path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
