from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext

from ...persistent_profiles import PersistentProfileHandle
from ...session_isolation import IsolatedBrowserRuntime

logger = logging.getLogger(__name__)


@dataclass
class PersistentProfileAttachment:
    browser: Browser
    context: BrowserContext
    handle: PersistentProfileHandle


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

    async def acquire_persistent_context(
        self,
        *,
        profile_name: str,
        context_kwargs: dict[str, Any],
        storage_state: dict[str, Any] | None,
    ) -> PersistentProfileAttachment:
        """Attach to (launching if needed) a named profile's persistent Chromium.

        browser-node owns the actual Chromium process -- it is the container
        with the X display the owner's noVNC view renders -- so this asks it
        to open (or reuse) the profile over its internal control API, then
        connects to the returned CDP endpoint. `connect_over_cdp` on an
        already-running persistent context exposes it as the browser's sole
        `contexts[0]`. The real teardown decision (refcounted across however
        many sessions hold this profile open) belongs to
        PersistentProfileClient.close, called explicitly before this Browser
        is closed -- whether closing this CDP-connected Browser object also
        happens to end the remote process or merely disconnects this client
        does not matter either way: browser-node's own `context.on('close')`
        handler reconciles its bookkeeping regardless of which side triggered
        the shutdown.
        """
        manager = self.manager
        if manager.playwright is None:
            raise RuntimeError("Playwright not started")
        handle = await manager.persistent_profiles.open(
            profile_name,
            context_kwargs=context_kwargs,
            storage_state=storage_state,
        )
        try:
            browser = await manager.playwright.chromium.connect_over_cdp(handle.cdp_endpoint)
        except Exception:
            # We told browser-node to open/hold this profile; if attaching to
            # it fails, release our hold rather than leaking a live Chromium
            # process nothing will ever use.
            await manager.persistent_profiles.close(profile_name)
            raise
        if not browser.contexts:
            await manager.persistent_profiles.close(profile_name)
            raise RuntimeError(f"persistent profile '{profile_name}' exposed no browser context over CDP")
        context = browser.contexts[0]
        return PersistentProfileAttachment(browser=browser, context=context, handle=handle)
