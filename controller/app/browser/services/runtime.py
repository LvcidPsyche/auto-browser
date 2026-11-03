from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from playwright.async_api import Browser

from ...action_errors import SESSION_CLOSED_GATE_LOST, SessionNotFoundError
from ...auth_policy import credentials_for
from ...browser_sandbox import refusal, snapshot_shared_state, verified_on
from ...session_isolation import IsolatedBrowserRuntime
from ...session_ownership import as_system
from ...utils import spawn_background_task
from ..request_gate import RequestGate, RequestGateUnavailable

logger = logging.getLogger(__name__)

# What shared_sandbox holds for a Chrome reached over CDP: readiness reports it as attached, not checked.
ATTACHED: dict[str, Any] = {"sandbox": "attached"}

# Closing the browser a CDP attach replaces must not hang the browser lock.
PREVIOUS_BROWSER_CLOSE_TIMEOUT_SECONDS = 10.0


class BrowserRuntimeService:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        # The shared browser-node's sandbox state as readiness and the on gate see it: the snapshot taken when the
        # controller connected (browser_sandbox.snapshot_shared_state), bound to that run's endpoint, while that
        # connection lasts; {"sandbox": "attached"} for a Chrome reached over CDP; None while not connected.
        self.shared_sandbox: dict[str, Any] | None = None
        # (endpoint, state) of the last snapshot. A reconnect to the same endpoint reuses it and never re-reads the
        # file, which the browser user can rewrite; a new endpoint (a new run) is read afresh.
        self._snapshot: tuple[str, dict[str, Any]] | None = None

    async def ensure_browser(self) -> Browser:
        manager = self.manager
        async with manager._browser_lock:
            if manager.browser is not None and manager.browser.is_connected():
                return manager.browser
            if manager.playwright is None:
                raise RuntimeError("Playwright not started")
            if manager.browser is not None:
                # Disconnected: its gate and window-birth watcher went with it.
                await manager.request_gates.release(manager.browser)
                await manager.window_births.release(manager.browser)

            if manager.settings.cdp_connect_url:
                self.shared_sandbox = None
                logger.info("connecting to existing Chrome via CDP at %s", manager.settings.cdp_connect_url)
                browser = await manager.playwright.chromium.connect_over_cdp(manager.settings.cdp_connect_url)
                logger.info("CDP attach succeeded")
                await manager.request_gates.attach(browser, attach_mode=True)
                manager.browser = browser
                # The operator's own Chrome: not a browser-node, so nothing to verify, and never called verified.
                self.shared_sandbox = dict(ATTACHED)
                self._watch_disconnect(browser)
                return manager.browser

            connected: dict[str, str] = {}

            async def endpoint() -> str:
                connected["endpoint"] = await self.resolve_browser_ws_endpoint()
                return connected["endpoint"]

            self.shared_sandbox = None
            manager.browser = await self.connect_browser(
                endpoint,
                failure_context=(
                    "Unable to connect to browser node via Playwright server. "
                    f"Checked ws endpoint file {manager.settings.browser_ws_endpoint_file} "
                    f"and direct endpoint {manager.settings.browser_ws_endpoint or '<not configured>'}."
                ),
            )
            endpoint_used = connected.get("endpoint", "")
            if self._snapshot is not None and self._snapshot[0] == endpoint_used:
                state = self._snapshot[1]  # the same run: never re-read
            else:
                state = snapshot_shared_state(manager.settings, endpoint_used)
                self._snapshot = (endpoint_used, state)
            self.shared_sandbox = state
            self._watch_disconnect(manager.browser)
            logger.info("shared browser-node sandbox: %s", self.shared_sandbox.get("sandbox"))
            return manager.browser

    def _watch_disconnect(self, browser: Any) -> None:
        """Report the snapshot only while its browser is connected: a dead connection's state describes nothing."""

        def disconnected(*_args: Any) -> None:
            if self.manager.browser is browser:
                self.shared_sandbox = None

        on = getattr(browser, "on", None)
        if callable(on):
            on("disconnected", disconnected)

    async def cdp_attach(self, cdp_url: str) -> dict[str, Any]:
        """Swap the browser every new session runs on for one reached over CDP.

        The browser is shared by all operators, so with more than one credential
        configured an attach would hand one operator's Chrome every other
        operator's sessions: refused. Otherwise it is refused while live
        sessions still use the current browser, since closing it would kill them.
        """
        manager = self.manager
        if len(credentials_for(manager.settings)) > 1:
            raise PermissionError(
                "browser.cdp_attach is disabled when more than one operator credential is configured: "
                "the attached browser would be shared by every operator. "
                "Run a separate controller per operator, or set CDP_CONNECT_URL at startup."
            )
        if manager.playwright is None:
            raise RuntimeError("Playwright not started")
        if manager.settings.browser_sandbox == "on":
            # An agent's tool, not the operator's configuration (CDP_CONNECT_URL): under on it must not move every new
            # session onto a Chrome the controller cannot verify.
            reason = (
                "browser.cdp_attach is refused under BROWSER_SANDBOX=on: the controller cannot verify the sandbox of "
                "a Chrome reached over CDP. Set CDP_CONNECT_URL at startup to use your own Chrome, or use "
                "BROWSER_SANDBOX=auto."
            )
            logger.warning("%s", reason)
            raise RuntimeError(reason)
        async with manager._browser_lock:
            in_use = self._sessions_on_shared_browser()
            if in_use:
                raise RuntimeError(
                    f"Cannot attach a new browser: close the {in_use} live session{'' if in_use == 1 else 's'} "
                    "using the current browser first, since replacing it would end them."
                )
            previous = manager.browser
            browser = await manager.playwright.chromium.connect_over_cdp(cdp_url)
            await manager.request_gates.attach(browser, attach_mode=True)
            manager.browser = browser
            self.shared_sandbox = dict(ATTACHED)
            self._watch_disconnect(browser)
            logger.info("attached to Chrome via CDP at %s", cdp_url)
            if previous is not None and previous is not browser:
                await manager.request_gates.release(previous)
                await self._close_previous_browser(previous)
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

    def _sessions_on_shared_browser(self) -> int:
        """Sessions, started or still starting, that run on manager.browser.

        An isolated session (docker_ephemeral) holds a browser of its own and
        does not depend on the shared one.
        """
        manager = self.manager
        shared = sum(1 for session in manager.sessions.values() if not session.owns_browser)
        return shared + manager.session_lifecycle._creating

    @staticmethod
    async def _close_previous_browser(browser: Browser) -> None:
        try:
            await asyncio.wait_for(browser.close(), timeout=PREVIOUS_BROWSER_CLOSE_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("could not close the browser replaced by cdp_attach: %s", exc, exc_info=True)

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
                # Playwright launched it (browser-node's launchServer): not an attach mode.
                await manager.request_gates.attach(browser, attach_mode=False)
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
            browser = await self.ensure_browser()
            # Under on nothing runs unsandboxed: the shared browser-node too, unless the controller attached to a
            # Chrome of the operator's own over CDP (not a browser-node, nothing to verify).
            if (
                manager.settings.browser_sandbox == "on"
                and not manager.settings.cdp_connect_url
                and self.shared_sandbox != ATTACHED
                and not verified_on(self.shared_sandbox)
            ):
                reason = refusal("the shared browser-node", self.shared_sandbox)
                # The API answers a bare 409; the operator reads the reason here.
                logger.warning("%s", reason)
                raise RuntimeError(reason)
            return browser, None

        try:
            runtime = await manager.runtime_provisioner.provision(session_id)
        except RuntimeError as exc:
            # A refusal under BROWSER_SANDBOX=on among them: the API answers a bare 409, so the reason is logged.
            logger.warning("isolated session %s was not started: %s", session_id, exc)
            raise
        try:
            browser = await self.connect_browser(
                lambda: asyncio.sleep(0, result=runtime.ws_endpoint),
                failure_context=(
                    "Unable to connect to isolated browser node via Playwright server. "
                    f"Checked isolated endpoint file {runtime.ws_endpoint_file}."
                ),
            )
            return browser, runtime
        except BaseException:  # cancellation included, or the container leaks
            await manager.runtime_provisioner.release(runtime)
            raise

    def gate_lost(self, gate: RequestGate) -> None:
        """A browser's pre-request gate detached on its own: Chromium resumes what it held (decision 2)."""
        spawn_background_task(self._contain_lost_gate(gate))

    async def _reattach(self, browser: Any) -> bool:
        """A replacement gate first, before any close: it inherits every owned context, so what still runs there is
        gated again within one attach, not after every close (decision 2a)."""
        try:
            replacement = await self.manager.request_gates.require(browser)
        except RequestGateUnavailable as exc:
            logger.error(
                "pre-request gate loss: no replacement gate attached (%s); closing the sessions, whose actions are "
                "refused meanwhile",
                exc.message,
            )
            return False
        return replacement is not None and replacement.live

    async def _end_survivors(self, browser: Any, failed: list[str], *, owned: bool) -> bool:
        """No context outlives containment. A shared browser is disconnected, which clears every context Playwright
        created there (an operator's own Chrome keeps running); ensure_browser reconnects at the next use. An owned
        browser was closed by its session's teardown, which keeps the gate attached when that close failed.
        Returns whether the browser was disconnected."""
        manager = self.manager
        names = ", ".join(failed)
        if owned:
            logger.error(
                "pre-request gate loss: session(s) %s did not close; their own browser's teardown closed it or kept "
                "its gate attached",
                names,
            )
            return False
        gates = manager.request_gates
        # As _close_owned_browser: the replacement gate stays attached, gating the survivors, until the disconnect
        # has succeeded, and the disconnect taking its session down is no loss.
        gates.ending(browser)
        try:
            await asyncio.wait_for(browser.close(), timeout=PREVIOUS_BROWSER_CLOSE_TIMEOUT_SECONDS)
        except Exception as exc:
            gates.ending(browser, False)
            logger.error(
                "pre-request gate loss: session(s) %s did not close, and disconnecting the shared browser failed "
                "(%s): the replacement gate stays attached, so their context stays gated",
                names,
                f"{type(exc).__name__}: {exc}"[:200],
            )
            return False
        await gates.release(browser)
        await self.manager.window_births.release(browser)
        logger.error(
            "pre-request gate loss: session(s) %s did not close; disconnected the shared browser, which clears "
            "their context, and the next session reconnects",
            names,
        )
        return True

    async def _contain_lost_gate(self, gate: RequestGate) -> None:
        """Close every session on the gate's browser; the next session there attaches a new gate.

        Not for a browser that disconnected: Playwright closes its contexts itself, and its sessions read as
        interrupted. What the gate held, or what started before the sessions closed, may have gone out.
        """
        manager = self.manager
        browser = gate.browser
        is_connected = getattr(browser, "is_connected", None)
        if callable(is_connected) and not is_connected():
            logger.info("pre-request gate ended with its browser's connection; nothing to contain")
            return
        sessions = [session for session in list(manager.sessions.values()) if session.browser is browser]
        logger.error(
            "pre-request gate detached from a live browser: closing its %d session(s) %s",
            len(sessions),
            ", ".join(session.id for session in sessions) or "-",
        )
        reattached = await self._reattach(browser)
        closed: list[str] = []
        failed: list[str] = []
        # Every operator's sessions, as shutdown closes them.
        with as_system():
            for session in sessions:
                try:
                    await manager.close_session(session.id, reason=SESSION_CLOSED_GATE_LOST)
                except SessionNotFoundError:
                    pass  # closed meanwhile by its own caller
                except Exception as exc:
                    logger.warning("could not close session %s after the gate detached: %s", session.id, exc)
                    failed.append(session.id)
                    continue
                # A context that did not really close (its close raised inside the teardown) stays owned, and
                # the next gate inherits it: still gated, but not closed.
                (failed if gate.owns(session.gate_context_id) else closed).append(session.id)
        owned = any(session.owns_browser for session in sessions if session.id in failed)
        disconnected = await self._end_survivors(browser, failed, owned=owned) if failed else False
        await manager.audit.append(
            event_type="request_gate_lost",
            status="error",
            action="request_gate",
            session_id=None,
            details={
                "closed_sessions": closed,
                "failed_sessions": failed,
                "reattached": reattached,
                "disconnected": disconnected,
            },
        )
