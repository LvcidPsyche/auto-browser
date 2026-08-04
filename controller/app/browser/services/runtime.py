from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from playwright.async_api import Browser

from ...session_isolation import IsolatedBrowserRuntime

logger = logging.getLogger(__name__)


class BrowserRuntimeService:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def ensure_browser(self) -> Browser:
        manager = self.manager
        async with manager._browser_lock:
            if manager.browser is not None and manager.browser.is_connected():
                return manager.browser
            if manager.playwright is None:
                raise RuntimeError("Playwright not started")

            if manager.settings.cdp_connect_url:
                logger.info("connecting to existing Chrome via CDP at %s", manager.settings.cdp_connect_url)
                manager.browser = await manager.playwright.chromium.connect_over_cdp(manager.settings.cdp_connect_url)
                logger.info("CDP attach succeeded")
                return manager.browser

            manager.browser = await self.connect_browser(
                self.resolve_browser_ws_endpoint,
                failure_context=(
                    "Unable to connect to browser node via Playwright server. "
                    f"Checked ws endpoint file {manager.settings.browser_ws_endpoint_file} "
                    f"and direct endpoint {manager.settings.browser_ws_endpoint or '<not configured>'}."
                ),
            )
            return manager.browser

    async def cdp_attach(self, cdp_url: str) -> dict[str, Any]:
        manager = self.manager
        if manager.playwright is None:
            raise RuntimeError("Playwright not started")
        async with manager._browser_lock:
            browser = await manager.playwright.chromium.connect_over_cdp(cdp_url)
            manager.browser = browser
            logger.info("attached to Chrome via CDP at %s", cdp_url)
            await manager.audit.append(
                event_type="cdp_attach",
                status="ok",
                action="cdp_attach",
                session_id=None,
                details={"cdp_url": cdp_url},
            )
            return {
                "attached": True,
                "cdp_url": cdp_url,
                "browser_version": browser.version,
            }

    async def connect_browser(self, ws_target_factory, *, failure_context: str) -> Browser:
        manager = self.manager
        if manager.playwright is None:
            raise RuntimeError("Playwright not started")

        last_error: Exception | None = None
        for attempt in range(1, manager.settings.connect_retries + 1):
            try:
                ws_target = await ws_target_factory()
                browser = await manager.playwright.chromium.connect(ws_target)
                logger.info(
                    "connected to browser node on attempt %s via playwright endpoint %s",
                    attempt,
                    ws_target,
                )
                return browser
            except Exception as exc:  # pragma: no cover - depends on external service
                last_error = exc
                await asyncio.sleep(manager.settings.connect_retry_delay_seconds)
        raise RuntimeError(failure_context) from last_error

    async def resolve_browser_ws_endpoint(self) -> str:
        manager = self.manager
        ws_endpoint_file = Path(manager.settings.browser_ws_endpoint_file)
        if ws_endpoint_file.exists():
            ws_endpoint = ws_endpoint_file.read_text(encoding="utf-8").strip()
            if ws_endpoint:
                return ws_endpoint
        if manager.settings.browser_ws_endpoint:
            return manager.settings.browser_ws_endpoint
        raise FileNotFoundError(f"missing playwright ws endpoint file: {ws_endpoint_file}")

    async def acquire_session_browser(self, session_id: str) -> tuple[Browser, IsolatedBrowserRuntime | None]:
        manager = self.manager
        if manager.settings.session_isolation_mode != "docker_ephemeral":
            return await self.ensure_browser(), None

        runtime = await manager.runtime_provisioner.provision(session_id)
        try:
            browser = await self.connect_browser(
                lambda: asyncio.sleep(0, result=runtime.ws_endpoint),
                failure_context=(
                    "Unable to connect to isolated browser node via Playwright server. "
                    f"Checked isolated endpoint file {runtime.ws_endpoint_file}."
                ),
            )
            return browser, runtime
        except Exception:
            await manager.runtime_provisioner.release(runtime)
            raise
