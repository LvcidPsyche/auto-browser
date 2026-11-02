"""Where the pre-request gate is attached, and what the controller does when it is missing or lost.

The gate (app/browser/request_gate.py) has to be on a browser before the controller opens its first
context there, in every way a browser is acquired: browser-node over chromium.connect, CDP_CONNECT_URL,
browser.cdp_attach, a docker_ephemeral container and the shadow launch. Each new context is owned from
its first page before that page navigates. Without a gate, sessions on that browser are refused while
PRE_REQUEST_BLOCKING is on (decision 7); a gate whose session detaches on its own closes every session on
its browser, and the next session attaches a new one (decision 2).
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import Error as PlaywrightError

from app.action_errors import BrowserActionError, SessionNotFoundError
from app.browser.launch_flags import STORAGE_PARTITIONING_ON
from app.browser.request_gate import RequestGateUnavailable
from app.browser_manager import BrowserManager
from tests._fake_cdp import FakeCDPSession, settle_background
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage, _settings

START_URL = "https://example.com/start"


def _browser() -> FakeBrowser:
    """A fake browser whose log orders the gate's commands against new_context and the page's goto."""
    page = FakePage()
    context = FakeContext(page)
    browser = FakeBrowser(context)
    browser.is_connected = lambda: True  # type: ignore[attr-defined]
    browser.close = AsyncMock()  # type: ignore[attr-defined]
    browser.version = "fake"  # type: ignore[attr-defined]

    def new_context(**_kwargs):
        browser.log.append(("new_context",))
        return context

    async def goto(url: str, wait_until: str = "domcontentloaded") -> None:
        browser.log.append(("goto", url))
        page.url = url

    browser.new_context.side_effect = new_context
    context.new_cdp_session = AsyncMock(side_effect=lambda _page: _registering(browser, context))
    page.goto = AsyncMock(side_effect=goto)
    return browser


def _registering(browser: FakeBrowser, context: FakeContext) -> FakeCDPSession:
    browser.log.append(("register",))
    return FakeCDPSession(
        {
            "Target.getTargetInfo": {
                "targetInfo": {"targetId": "first-page", "type": "page", "browserContextId": context.context_id}
            }
        }
    )


def _gate_sessions(browser: FakeBrowser) -> list[FakeCDPSession]:
    """The browser sessions that enabled Fetch: gates, not the prefetch preference's short-lived session."""
    return [session for session in browser.cdp_sessions if any(m == "Fetch.enable" for m, _ in session.sent)]


class GateWiringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = BrowserManager(_settings(self.root))
        self.manager.audit.append = AsyncMock()  # type: ignore[method-assign]
        self.manager._persist_session = AsyncMock()  # type: ignore[method-assign]
        self.manager._settle = AsyncMock()  # type: ignore[method-assign]
        self.manager._maybe_provision_session_tunnel = AsyncMock()  # type: ignore[method-assign]
        self.browser = _browser()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def _playwright(self, **chromium) -> None:
        self.manager.playwright = SimpleNamespace(chromium=SimpleNamespace(**chromium))  # type: ignore[assignment]

    def _assert_gate_first(self, browser: FakeBrowser) -> None:
        """The gate attached, then the context, then its first page owned, then the first navigation."""
        steps = [entry[1] if entry[0] == "cdp" else entry[0] for entry in browser.log]
        order = ["Target.setDiscoverTargets", "Fetch.enable", "new_context", "register", "goto"]
        firsts = [steps.index(step) if step in steps else -1 for step in order]
        self.assertTrue(-1 not in firsts and firsts == sorted(firsts), browser.log)

    # ── each way a browser is acquired ───────────────────────────────────────

    async def test_browser_node_over_chromium_connect(self) -> None:
        self.manager.settings.browser_ws_endpoint = "ws://browser-node:9223/x"
        self._playwright(connect=AsyncMock(return_value=self.browser))

        await self.manager.ensure_browser()
        attached = list(self.browser.log)
        session_id = (await self.manager.create_session(name="s", start_url=START_URL))["id"]

        # The gate, then the prefetch preference of a launched browser (decision 5), all at acquisition.
        self.assertEqual(
            [m for _, m in attached][:3], ["Target.setDiscoverTargets", "Fetch.enable", "Target.createTarget"]
        )
        self.assertIn(
            ("Target.createTarget", {"url": "chrome://settings/performance"}), self.browser.cdp_sessions[1].sent
        )
        self._assert_gate_first(self.browser)
        gate = self.manager.request_gates.get(self.browser)
        self.assertFalse(gate.attach_mode)
        self.assertTrue(gate.owns(self.manager.sessions[session_id].gate_context_id))
        self.assertEqual(len(_gate_sessions(self.browser)), 1, "create_session attached a second gate")

    async def test_cdp_connect_url_is_an_attach_mode(self) -> None:
        self.manager.settings.cdp_connect_url = "http://127.0.0.1:9222"
        self._playwright(connect_over_cdp=AsyncMock(return_value=self.browser))

        await self.manager.ensure_browser()
        await self.manager.create_session(name="s", start_url=START_URL)

        self._assert_gate_first(self.browser)
        self.assertTrue(self.manager.request_gates.get(self.browser).attach_mode)
        sent = [method for session in self.browser.cdp_sessions for method, _ in session.sent]
        self.assertNotIn("Target.createTarget", sent, "the operator's own prefetch setting was touched")

    async def test_cdp_attach_gates_the_new_browser_and_releases_the_old_one(self) -> None:
        previous = _browser()
        self.manager.browser = previous  # type: ignore[assignment]
        await self.manager.request_gates.attach(previous, attach_mode=False)
        old_gate = self.manager.request_gates.get(previous)
        self._playwright(connect_over_cdp=AsyncMock(return_value=self.browser))

        await self.manager.cdp_attach("http://127.0.0.1:9222")
        await self.manager.create_session(name="s", start_url=START_URL)

        self._assert_gate_first(self.browser)
        self.assertTrue(self.manager.request_gates.get(self.browser).attach_mode)
        self.assertFalse(old_gate.live)
        self.assertTrue(previous.cdp_sessions[0].detached)
        self.assertIsNone(self.manager.request_gates.get(previous))
        previous.close.assert_awaited_once()

    async def test_docker_ephemeral_gates_each_isolated_browser(self) -> None:
        self.manager.settings.session_isolation_mode = "docker_ephemeral"
        runtime = SimpleNamespace(
            ws_endpoint="ws://isolated:9223/x",
            ws_endpoint_file=self.root / "ws.txt",
            takeover_url="http://127.0.0.1:6080/",
            browser_node_name="isolated-1",
            container_id="c1",
            container_name="n1",
            network_name="net",
            profile_dir=self.root / "profile",
            downloads_dir=self.root / "downloads",
            novnc_port=None,
        )
        self.manager.runtime_provisioner.provision = AsyncMock(return_value=runtime)  # type: ignore[method-assign]
        self.manager.runtime_provisioner.release = AsyncMock()  # type: ignore[method-assign]
        self._playwright(connect=AsyncMock(return_value=self.browser))

        session_id = (await self.manager.create_session(name="s", start_url=START_URL))["id"]

        self._assert_gate_first(self.browser)
        self.assertFalse(self.manager.request_gates.get(self.browser).attach_mode)

        await self.manager.close_session(session_id)

        self.browser.close.assert_awaited_once()
        self.assertTrue(self.browser.cdp_sessions[0].detached, "the closed browser's gate was not released")
        self.assertIsNone(self.manager.request_gates.get(self.browser))

    async def test_the_shadow_launch_gates_its_headed_browser(self) -> None:
        self.manager.settings.shadow_browse_enabled = True
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        source_id = (await self.manager.create_session(name="source", start_url=START_URL))["id"]
        self.manager.sessions[source_id].context.storage_state = AsyncMock(return_value={})
        headed = _browser()
        launch = AsyncMock(return_value=headed)
        self._playwright(launch=launch)

        shadow_id = (await self.manager.enable_shadow_browse(source_id))["shadow_session_id"]

        # Decision 9: storage partitioning back on, Playwright's other disabled features kept.
        self.assertEqual(launch.await_args.kwargs["args"][-1], STORAGE_PARTITIONING_ON)
        self._assert_gate_first(headed)
        gate = self.manager.request_gates.get(headed)
        self.assertFalse(gate.attach_mode)
        self.assertTrue(gate.owns(self.manager.sessions[shadow_id].gate_context_id))

    async def test_a_browser_acquired_some_other_way_is_gated_at_create(self) -> None:
        """A browser the gate has not seen (a reconnect it missed) is attached before its first context."""
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]

        await self.manager.create_session(name="s", start_url=START_URL)

        self._assert_gate_first(self.browser)

    # ── decision 7: no gate, no session ──────────────────────────────────────

    async def test_a_browser_the_gate_cannot_attach_to_gets_no_sessions(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]

        async def refusing() -> FakeCDPSession:
            return FakeCDPSession({"Fetch.enable": RuntimeError("Fetch.enable failed")})

        self.browser.new_browser_cdp_session.side_effect = refusing

        with self.assertRaisesRegex(RequestGateUnavailable, "PRE_REQUEST_BLOCKING"):
            await self.manager.create_session(name="s", start_url=START_URL)

        self.browser.new_context.assert_not_awaited()
        self.assertEqual(self.manager.sessions, {})

    async def test_an_attach_failure_at_acquisition_is_logged_and_sessions_are_refused(self) -> None:
        self.manager.settings.browser_ws_endpoint = "ws://browser-node:9223/x"
        self._playwright(connect=AsyncMock(return_value=self.browser))
        self.browser.new_browser_cdp_session.side_effect = RuntimeError("no browser target")

        with self.assertLogs("app.browser.request_gates", level="WARNING") as logs:
            await self.manager.ensure_browser()
        self.assertIn("no browser target", "\n".join(logs.output))

        with self.assertRaises(RequestGateUnavailable):
            await self.manager.create_session(name="s")
        self.browser.new_context.assert_not_awaited()

    async def test_a_context_the_gate_cannot_own_is_closed_and_refused(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        context = self.browser.context
        context.new_cdp_session = AsyncMock(
            return_value=FakeCDPSession({"Target.getTargetInfo": RuntimeError("Target closed")})
        )

        with self.assertRaisesRegex(RequestGateUnavailable, "Target closed"):
            await self.manager.create_session(name="s", start_url=START_URL)

        context.close.assert_awaited()
        context.page.goto.assert_not_awaited()
        self.assertEqual(self.manager.sessions, {})

    async def test_pre_request_blocking_off_opens_no_gate(self) -> None:
        self.manager.settings.pre_request_blocking = False
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]

        session_id = (await self.manager.create_session(name="s", start_url=START_URL))["id"]

        self.browser.new_browser_cdp_session.assert_not_awaited()
        self.assertIsNone(self.manager.sessions[session_id].gate_context_id)

    async def test_an_allowlist_that_allows_every_host_opens_no_gate(self) -> None:
        self.manager.settings.allowed_hosts = "*"
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]

        await self.manager.create_session(name="s", start_url=START_URL)

        self.browser.new_browser_cdp_session.assert_not_awaited()

    # ── decision 2: a gate that detaches on its own ──────────────────────────

    async def _two_sessions(self) -> tuple[str, str]:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        first = (await self.manager.create_session(name="one"))["id"]
        self.browser.context = FakeContext(FakePage())
        self.browser.new_context.side_effect = lambda **_: self.browser.context
        second = (await self.manager.create_session(name="two"))["id"]
        return first, second

    async def _settled(self) -> None:
        await settle_background()

    async def test_a_lost_gate_closes_every_session_on_its_browser(self) -> None:
        self.manager.settings.max_sessions = 3
        first, second = await self._two_sessions()
        other = _browser()
        self.manager._acquire_session_browser = AsyncMock(return_value=(other, None))  # type: ignore[method-assign]
        elsewhere = (await self.manager.create_session(name="elsewhere"))["id"]

        self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
        await self._settled()

        self.assertNotIn(first, self.manager.sessions)
        self.assertNotIn(second, self.manager.sessions)
        self.assertIn(elsewhere, self.manager.sessions, "a session on another browser was closed")
        [lost] = [
            c.kwargs
            for c in self.manager.audit.append.await_args_list
            if c.kwargs.get("event_type") == "request_gate_lost"
        ]
        self.assertEqual(sorted(lost["details"]["closed_sessions"]), sorted([first, second]))
        self.assertEqual(lost["details"]["failed_sessions"], [])

    async def test_the_next_session_after_a_loss_attaches_a_new_gate(self) -> None:
        first, _ = await self._two_sessions()
        lost = self.manager.request_gates.get(self.browser)
        self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
        await self._settled()
        self.browser.context = FakeContext(FakePage())
        self.browser.new_context.side_effect = lambda **_: self.browser.context

        session_id = (await self.manager.create_session(name="after"))["id"]

        gate = self.manager.request_gates.get(self.browser)
        self.assertIsNot(gate, lost)
        self.assertTrue(gate.live)
        self.assertEqual(len(self.browser.cdp_sessions), 2)
        self.assertTrue(gate.owns(self.manager.sessions[session_id].gate_context_id))

    async def test_a_session_still_starting_when_the_gate_is_lost_is_refused(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        page = self.browser.context.page

        async def goto_and_lose_the_gate(url: str, wait_until: str = "domcontentloaded") -> None:
            page.url = url
            self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])

        page.goto = AsyncMock(side_effect=goto_and_lose_the_gate)

        with self.assertRaisesRegex(RequestGateUnavailable, "detached"):
            await self.manager.create_session(name="s", start_url=START_URL)
        await self._settled()

        self.assertEqual(self.manager.sessions, {})
        self.browser.context.close.assert_awaited()

    async def test_a_browser_that_disconnected_is_left_to_playwright(self) -> None:
        """A whole-connection drop closes the contexts itself; the sessions read as interrupted."""
        first, second = await self._two_sessions()
        self.browser.is_connected = lambda: False  # type: ignore[attr-defined]

        self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
        await self._settled()

        self.assertIn(first, self.manager.sessions)
        self.assertIn(second, self.manager.sessions)

    async def test_closing_a_session_does_not_release_the_shared_browsers_gate(self) -> None:
        first, second = await self._two_sessions()
        closed_context = self.manager.sessions[first].gate_context_id
        gate = self.manager.request_gates.get(self.browser)
        self.assertTrue(gate.owns(closed_context))

        await self.manager.close_session(first)

        self.assertTrue(gate.live)
        self.assertIs(self.manager.request_gates.get(self.browser), gate)
        self.assertIn(second, self.manager.sessions)
        self.assertFalse(gate.owns(closed_context), "the closed session's context is still owned")
        self.assertTrue(gate.owns(self.manager.sessions[second].gate_context_id))

    # ── checkpoint 1 corrections and review findings ─────────────────────────

    async def test_an_empty_allowlist_opens_no_gate(self) -> None:
        """An empty ALLOWED_HOSTS allows every host (_assert_url_allowed returns early)."""
        self.manager.settings.allowed_hosts = ""
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]

        await self.manager.create_session(name="s", start_url=START_URL)

        self.browser.new_browser_cdp_session.assert_not_awaited()

    async def test_concurrent_requires_attach_one_gate(self) -> None:
        async def slow_session() -> FakeCDPSession:
            await asyncio.sleep(0.01)
            session = FakeCDPSession(log=self.browser.log)
            self.browser.cdp_sessions.append(session)
            return session

        self.browser.new_browser_cdp_session.side_effect = slow_session

        first, second = await asyncio.gather(
            self.manager.request_gates.require(self.browser), self.manager.request_gates.require(self.browser)
        )

        self.assertIs(first, second)
        self.assertEqual(len(_gate_sessions(self.browser)), 1, "two Fetch sessions on one browser")

    async def test_a_create_that_fails_after_registering_leaves_the_context_unowned(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        self.browser.context.page.goto = AsyncMock(side_effect=RuntimeError("net::ERR_CONNECTION_REFUSED"))

        with self.assertRaisesRegex(RuntimeError, "ERR_CONNECTION_REFUSED"):
            await self.manager.create_session(name="s", start_url=START_URL)

        gate = self.manager.request_gates.get(self.browser)
        self.assertTrue(gate.live)
        self.assertFalse(gate.owns(self.browser.context.context_id))

    async def test_a_failed_shadow_launch_releases_its_browsers_gate(self) -> None:
        self.manager.settings.shadow_browse_enabled = True
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        source_id = (await self.manager.create_session(name="source", start_url=START_URL))["id"]
        self.manager.sessions[source_id].context.storage_state = AsyncMock(return_value={})
        headed = _browser()
        headed.context.page.goto = AsyncMock(side_effect=RuntimeError("shadow goto failed"))
        self._playwright(launch=AsyncMock(return_value=headed))

        with self.assertRaisesRegex(RuntimeError, "shadow goto failed"):
            await self.manager.enable_shadow_browse(source_id)

        self.assertIsNone(self.manager.request_gates.get(headed))
        self.assertTrue(headed.cdp_sessions[0].detached)
        headed.close.assert_awaited_once()

    async def test_a_session_whose_containment_close_fails_is_still_closed(self) -> None:
        first, _ = await self._two_sessions()
        session = self.manager.sessions[first]
        self.manager._session_summary = AsyncMock(side_effect=RuntimeError("summary failed"))  # type: ignore[method-assign]

        with self.assertLogs("app.browser.services.runtime", level="WARNING") as logs:
            self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
            await self._settled()

        self.assertNotIn(first, self.manager.sessions)
        session.context.close.assert_awaited()
        self.assertIn("summary failed", " ".join(logs.output))
        [lost] = [
            c.kwargs
            for c in self.manager.audit.append.await_args_list
            if c.kwargs.get("event_type") == "request_gate_lost"
        ]
        self.assertIn(first, lost["details"]["failed_sessions"])
        self.assertNotIn(first, lost["details"]["closed_sessions"])

    async def test_containment_reports_a_session_whose_context_did_not_close(self) -> None:
        """X3 (b): close_session returned, but the context's close raised inside the teardown: not "closed"."""
        first, second = await self._two_sessions()
        self.manager.sessions[first].context.close = AsyncMock(side_effect=RuntimeError("context close failed"))

        self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
        await self._settled()

        [lost] = [
            c.kwargs
            for c in self.manager.audit.append.await_args_list
            if c.kwargs.get("event_type") == "request_gate_lost"
        ]
        self.assertEqual((lost["details"]["failed_sessions"], lost["details"]["closed_sessions"]), ([first], [second]))

    async def test_close_releases_the_context_even_when_the_summary_fails(self) -> None:
        """X3 (a): the summary raised, the session was dropped, and its context stayed open with its slot freed."""
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        session_id = (await self.manager.create_session(name="s"))["id"]
        context = self.manager.sessions[session_id].context
        self.manager._session_summary = AsyncMock(side_effect=RuntimeError("summary failed"))  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "summary failed"):
            await self.manager.close_session(session_id)

        self.assertNotIn(session_id, self.manager.sessions)
        context.close.assert_awaited_once()

    async def test_a_context_whose_close_fails_stays_owned(self) -> None:
        """X2: ownership ends when the context has closed, not when the controller starts closing it."""
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        session_id = (await self.manager.create_session(name="s"))["id"]
        session = self.manager.sessions[session_id]
        session.context.close = AsyncMock(side_effect=RuntimeError("context close failed"))

        await self.manager.close_session(session_id)

        self.assertTrue(self.manager.request_gates.get(self.browser).owns(session.gate_context_id))

    async def test_a_refused_request_during_a_slow_context_close_is_failed(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        session_id = (await self.manager.create_session(name="s"))["id"]
        session = self.manager.sessions[session_id]
        cdp = self.browser.cdp_sessions[0]
        cdp.replies["Target.getTargetInfo"] = {
            "targetInfo": {"targetId": "closing-page", "type": "page", "browserContextId": session.gate_context_id}
        }
        release = asyncio.Event()
        finish_close = session.context.close.side_effect

        async def slow_close() -> None:
            await release.wait()
            await finish_close()

        session.context.close = AsyncMock(side_effect=slow_close)
        closing = asyncio.create_task(self.manager.close_session(session_id))
        await self._settled()

        cdp.emit(
            "Fetch.requestPaused",
            {
                "requestId": "late",
                "request": {"url": "https://refused.example/", "method": "GET", "headers": {}},
                "frameId": "closing-page",
            },
        )
        method, _ = await cdp.wait_for_reply("late")
        release.set()
        await closing

        self.assertEqual(method, "Fetch.failRequest")
        self.assertFalse(self.manager.request_gates.get(self.browser).owns(session.gate_context_id))

    async def test_shutdown_releases_the_gates_without_a_loss(self) -> None:
        self.manager.settings.browser_ws_endpoint = "ws://browser-node:9223/x"
        self.manager.playwright = SimpleNamespace(  # type: ignore[assignment]
            chromium=SimpleNamespace(connect=AsyncMock(return_value=self.browser)), stop=AsyncMock()
        )
        await self.manager.ensure_browser()
        gate = self.manager.request_gates.get(self.browser)
        self.manager.tunnel_broker.shutdown = AsyncMock()  # type: ignore[method-assign]
        self.manager.witness_remote.shutdown = AsyncMock()  # type: ignore[method-assign]
        self.manager.session_store.shutdown = AsyncMock()  # type: ignore[method-assign]

        await self.manager.shutdown()
        await self._settled()

        self.assertFalse(gate.live)
        self.assertTrue(self.browser.cdp_sessions[0].detached)
        events = [call.kwargs.get("event_type") for call in self.manager.audit.append.await_args_list]
        self.assertNotIn("request_gate_lost", events)

    # ── round 3: no context runs ungated ─────────────────────────────────────

    async def test_r1_a_shadow_launch_whose_gate_is_lost_while_it_starts_is_refused(self) -> None:
        """The loss lands before the shadow session is in manager.sessions, so containment cannot see it."""
        self.manager.settings.shadow_browse_enabled = True
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        source_id = (await self.manager.create_session(name="source", start_url=START_URL))["id"]
        self.manager.sessions[source_id].context.storage_state = AsyncMock(return_value={})
        headed = _browser()
        self._playwright(launch=AsyncMock(return_value=headed))
        gates = self.manager.request_gates

        async def lose_the_gate_meanwhile(_page) -> None:
            if gates.get(headed) is not None:
                [gate_session] = _gate_sessions(headed)
                gate_session.emit("close", gate_session)
                await self._settled()  # containment runs now, and finds no session on the browser

        gates.bypass_service_workers = AsyncMock(side_effect=lose_the_gate_meanwhile)  # type: ignore[method-assign]

        with self.assertRaises(RequestGateUnavailable):
            await self.manager.enable_shadow_browse(source_id)

        self.assertEqual(
            [s for s in self.manager.sessions.values() if s.browser is headed], [], "a shadow session runs ungated"
        )
        headed.close.assert_awaited_once()

    async def test_f1_a_reattach_that_fails_still_closes_the_sessions(self) -> None:
        """Their actions are refused meanwhile (F2); the failure is logged at ERROR."""
        first, second = await self._two_sessions()
        self.browser.new_browser_cdp_session.side_effect = RuntimeError("no more sessions")

        with self.assertLogs("app.browser.services.runtime", level="ERROR") as logs:
            self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
            await self._settled()

        self.assertNotIn(first, self.manager.sessions)
        self.assertNotIn(second, self.manager.sessions)
        self.assertIn("no replacement gate attached", " ".join(logs.output))
        [lost] = [
            c.kwargs
            for c in self.manager.audit.append.await_args_list
            if c.kwargs.get("event_type") == "request_gate_lost"
        ]
        self.assertFalse(lost["details"]["reattached"])

    async def test_r3_prefetch_is_turned_off_even_when_the_first_attach_fails(self) -> None:
        self.manager.settings.browser_ws_endpoint = "ws://browser-node:9223/x"
        self._playwright(connect=AsyncMock(return_value=self.browser))
        attaches = 0

        async def failing_first() -> FakeCDPSession:
            nonlocal attaches
            attaches += 1
            replies = {"Fetch.enable": RuntimeError("Fetch.enable failed")} if attaches == 1 else {}
            session = FakeCDPSession(replies, log=self.browser.log)
            self.browser.cdp_sessions.append(session)
            return session

        self.browser.new_browser_cdp_session.side_effect = failing_first
        prefetch = AsyncMock(return_value=2)

        with patch("app.browser.request_gates.turn_prefetch_off", prefetch):
            await self.manager.ensure_browser()
            await self.manager.create_session(name="s", start_url=START_URL)

        prefetch.assert_awaited_once_with(self.browser)
        [gate] = self.manager.request_gates.status()["gates"]
        self.assertEqual((gate["state"], gate["prefetch_pref"]), ("attached", 2))

    async def test_r14_a_shadow_launch_whose_page_the_gate_stops_is_the_same_block(self) -> None:
        self.manager.settings.shadow_browse_enabled = True
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        source_id = (await self.manager.create_session(name="source", start_url=START_URL))["id"]
        self.manager.sessions[source_id].context.storage_state = AsyncMock(return_value={})
        headed = _browser()
        headed.context.page.context = headed.context  # as a Playwright page has
        self._playwright(launch=AsyncMock(return_value=headed))

        async def stopped_by_the_gate(url: str, wait_until: str = "domcontentloaded") -> None:
            [gate_session] = _gate_sessions(headed)
            gate_session.replies["Target.getTargetInfo"] = {
                "targetInfo": {"targetId": "first-page", "type": "page", "browserContextId": headed.context.context_id}
            }
            gate_session.emit(
                "Fetch.requestPaused",
                {
                    "requestId": "shadow-hop",
                    "frameId": "first-page",
                    "request": {"url": "https://refused.example/secret", "method": "GET", "headers": {}},
                },
            )
            await gate_session.wait_for_reply("shadow-hop")
            raise PlaywrightError(f"Page.goto: net::ERR_BLOCKED_BY_CLIENT at {url}")

        headed.context.page.goto = AsyncMock(side_effect=stopped_by_the_gate)

        with self.assertRaises(BrowserActionError) as caught:
            await self.manager.enable_shadow_browse(source_id)

        self.assertEqual(
            (caught.exception.code, caught.exception.action), ("browser_action_blocked", "enable_shadow_browse")
        )
        self.assertNotIn("secret", caught.exception.message)
        headed.close.assert_awaited_once()

    # ── round 4: F1, containment reattaches first and leaves no context running ──

    def _isolated(self) -> None:
        self.manager.settings.session_isolation_mode = "docker_ephemeral"
        runtime = SimpleNamespace(
            ws_endpoint="ws://isolated:9223/x",
            ws_endpoint_file=self.root / "ws.txt",
            takeover_url="http://127.0.0.1:6080/",
            browser_node_name="isolated-1",
            container_id="c1",
            container_name="n1",
            network_name="net",
            profile_dir=self.root / "profile",
            downloads_dir=self.root / "downloads",
            novnc_port=None,
        )
        self.manager.runtime_provisioner.provision = AsyncMock(return_value=runtime)  # type: ignore[method-assign]
        self.manager.runtime_provisioner.release = AsyncMock()  # type: ignore[method-assign]
        self._playwright(connect=AsyncMock(return_value=self.browser))

    async def test_f1_containment_reattaches_before_its_first_close(self) -> None:
        await self._two_sessions()
        order: list[str] = []
        attach = self.browser.new_browser_cdp_session.side_effect

        async def logged_attach() -> FakeCDPSession:
            order.append("attach")
            return await attach()

        self.browser.new_browser_cdp_session.side_effect = logged_attach
        close = self.manager.close_session

        async def logged_close(session_id: str, **kwargs):
            order.append("close")
            return await close(session_id, **kwargs)

        self.manager.close_session = logged_close  # type: ignore[method-assign]
        self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
        await self._settled()

        self.assertEqual(order[:2], ["attach", "close"], order)
        self.assertTrue(self.manager.request_gates.get(self.browser).live)

    async def test_f1_a_shared_browser_with_an_unclosable_context_is_disconnected_and_replaced(self) -> None:
        self.manager.settings.browser_ws_endpoint = "ws://browser-node:9223/x"
        fresh = _browser()
        self._playwright(connect=AsyncMock(side_effect=[self.browser, fresh]))
        connected = {"yes": True}
        self.browser.is_connected = lambda: connected["yes"]  # type: ignore[attr-defined]

        async def disconnect() -> None:
            connected["yes"] = False

        self.browser.close = AsyncMock(side_effect=disconnect)  # type: ignore[attr-defined]
        first = (await self.manager.create_session(name="one"))["id"]
        self.manager.sessions[first].context.close = AsyncMock(side_effect=RuntimeError("context close failed"))

        self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
        await self._settled()

        self.browser.close.assert_awaited_once()
        self.assertIsNone(self.manager.request_gates.get(self.browser), "the disconnected browser kept a gate")
        [lost] = [
            c.kwargs
            for c in self.manager.audit.append.await_args_list
            if c.kwargs.get("event_type") == "request_gate_lost"
        ]
        self.assertTrue(lost["details"]["disconnected"])
        self.assertEqual(lost["details"]["failed_sessions"], [first])

        session_id = (await self.manager.create_session(name="after"))["id"]

        session = self.manager.sessions[session_id]
        self.assertIs(session.browser, fresh)
        gate = self.manager.request_gates.get(fresh)
        self.assertTrue(gate.live and gate.owns(session.gate_context_id))

    async def test_f1_an_owned_browser_that_will_not_close_keeps_its_gate(self) -> None:
        self._isolated()
        session_id = (await self.manager.create_session(name="s", start_url=START_URL))["id"]
        session = self.manager.sessions[session_id]
        session.context.close = AsyncMock(side_effect=RuntimeError("context close failed"))
        self.browser.close = AsyncMock(side_effect=RuntimeError("browser close failed"))  # type: ignore[attr-defined]

        with self.assertLogs("app.browser.services.sessions", level="ERROR") as logs:
            await self.manager.close_session(session_id)

        gate = self.manager.request_gates.get(self.browser)
        self.assertTrue(gate is not None and gate.live, "the gate of a browser still running was released")
        self.assertIn("did not close", " ".join(logs.output))
        cdp = self.browser.cdp_sessions[0]
        cdp.replies["Target.getTargetInfo"] = {
            "targetInfo": {"targetId": "left", "type": "page", "browserContextId": session.gate_context_id}
        }
        cdp.emit(
            "Fetch.requestPaused",
            {"requestId": "r1", "frameId": "left", "request": {"url": "https://refused.example/", "headers": {}}},
        )
        method, _ = await cdp.wait_for_reply("r1")
        self.assertEqual(method, "Fetch.failRequest")

    async def test_f1_an_owned_browser_whose_close_takes_its_gate_down_reports_no_loss(self) -> None:
        self._isolated()
        session_id = (await self.manager.create_session(name="s", start_url=START_URL))["id"]
        gate_session = self.browser.cdp_sessions[0]

        async def close_takes_the_gate_down() -> None:
            gate_session.emit("close", gate_session)  # as a real browser close ends every CDP session

        self.browser.close = AsyncMock(side_effect=close_takes_the_gate_down)  # type: ignore[attr-defined]

        await self.manager.close_session(session_id)
        await self._settled()

        events = [c.kwargs.get("event_type") for c in self.manager.audit.append.await_args_list]
        self.assertNotIn("request_gate_lost", events)
        self.assertIsNone(self.manager.request_gates.get(self.browser))

    async def test_f1_a_failed_shadow_launch_closes_its_context_before_releasing_its_gate(self) -> None:
        self.manager.settings.shadow_browse_enabled = True
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        source_id = (await self.manager.create_session(name="source", start_url=START_URL))["id"]
        self.manager.sessions[source_id].context.storage_state = AsyncMock(return_value={})
        headed = _browser()
        headed.context.page.goto = AsyncMock(side_effect=RuntimeError("shadow goto failed"))
        self._playwright(launch=AsyncMock(return_value=headed))
        order: list[str] = []
        close_context = headed.context.close.side_effect

        async def logged_context_close() -> None:
            order.append("context closed")
            await close_context()

        headed.context.close = AsyncMock(side_effect=logged_context_close)
        release = self.manager.request_gates.release

        async def logged_release(browser) -> None:
            order.append("gate released")
            await release(browser)

        self.manager.request_gates.release = logged_release  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "shadow goto failed"):
            await self.manager.enable_shadow_browse(source_id)

        self.assertEqual(order, ["context closed", "gate released"])

    # ── round 4: F2, nothing the controller starts runs on a lost gate ───────

    async def test_f2_an_action_on_a_session_whose_gate_just_closed_is_refused_before_it_runs(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        session_id = (await self.manager.create_session(name="s"))["id"]
        page = self.manager.sessions[session_id].page
        self.manager.runtime.gate_lost = lambda _gate: None  # type: ignore[method-assign]  # containment not yet run
        self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
        page.goto.reset_mock()

        with self.assertRaises(SessionNotFoundError) as caught:
            await self.manager.navigate(session_id, "https://example.com/next")

        self.assertEqual((caught.exception.code, caught.exception.reason), ("session_closed", "request_gate_lost"))
        page.goto.assert_not_awaited()

    async def test_f2_a_tab_opened_on_a_lost_gate_is_refused_before_it_exists(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        session_id = (await self.manager.create_session(name="s"))["id"]
        context = self.manager.sessions[session_id].context
        self.manager.runtime.gate_lost = lambda _gate: None  # type: ignore[method-assign]
        self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
        context.new_page.reset_mock()

        with self.assertRaises(SessionNotFoundError):
            await self.manager.open_tab(session_id, "https://example.com/tab", True)

        context.new_page.assert_not_awaited()

    async def test_f2_an_action_queued_behind_a_containment_close_says_why(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        session_id = (await self.manager.create_session(name="s"))["id"]
        session = self.manager.sessions[session_id]
        release = asyncio.Event()
        finish_close = session.context.close.side_effect

        async def slow_close() -> None:
            await release.wait()
            await finish_close()

        session.context.close = AsyncMock(side_effect=slow_close)
        closing = asyncio.create_task(self.manager.close_session(session_id, reason="request_gate_lost"))
        await self._settled()
        queued = asyncio.create_task(self.manager.navigate(session_id, "https://example.com/next"))
        await self._settled()
        session.page.goto.reset_mock()
        release.set()
        await closing

        with self.assertRaises(SessionNotFoundError) as caught:
            await queued

        self.assertEqual((caught.exception.code, caught.exception.reason), ("session_closed", "request_gate_lost"))
        session.page.goto.assert_not_awaited()

    async def test_f8g_a_close_whose_summary_fails_still_records_it_as_closed_with_its_reason(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        session_id = (await self.manager.create_session(name="s"))["id"]
        self.manager._session_summary = AsyncMock(side_effect=RuntimeError("summary failed"))  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "summary failed"):
            await self.manager.close_session(session_id, reason="request_gate_lost")

        with self.assertRaises(SessionNotFoundError) as caught:
            await self.manager.get_session(session_id)
        self.assertEqual((caught.exception.status, caught.exception.reason), ("closed", "request_gate_lost"))

    # ── round 4: F9, the agent contract ──────────────────────────────────────

    async def test_f9c_a_start_url_that_leaves_for_a_refused_host_while_create_settles_is_a_block(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        page = self.browser.context.page
        page.context = self.browser.context

        async def settles_then_leaves(_page) -> None:
            [gate_session] = _gate_sessions(self.browser)
            gate_session.replies["Target.getTargetInfo"] = {
                "targetInfo": {
                    "targetId": "start-page",
                    "type": "page",
                    "browserContextId": self.browser.context.context_id,
                }
            }
            gate_session.emit(
                "Fetch.requestPaused",
                {
                    "requestId": "meta-refresh",
                    "frameId": "start-page",
                    "request": {"url": "https://refused.example/secret", "method": "GET", "headers": {}},
                },
            )
            await gate_session.wait_for_reply("meta-refresh")

        self.manager._settle = settles_then_leaves  # type: ignore[method-assign]
        self.manager.request_gates.target_id_of = AsyncMock(return_value="start-page")  # type: ignore[method-assign]

        with self.assertRaises(BrowserActionError) as caught:
            await self.manager.create_session(name="s", start_url=START_URL)

        self.assertEqual(
            (caught.exception.code, caught.exception.action, caught.exception.url),
            ("browser_action_blocked", "create_session", "https://refused.example"),
        )
        self.assertEqual(self.manager.sessions, {})

    async def test_f9c_a_shadow_page_that_leaves_for_a_refused_host_while_it_settles_is_a_block(self) -> None:
        self.manager.settings.shadow_browse_enabled = True
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        source_id = (await self.manager.create_session(name="source", start_url=START_URL))["id"]
        self.manager.sessions[source_id].context.storage_state = AsyncMock(return_value={})
        headed = _browser()
        headed.context.page.context = headed.context
        self._playwright(launch=AsyncMock(return_value=headed))
        settle = self.manager._settle

        async def settles_then_leaves(page) -> None:
            if page is not headed.context.page:
                return await settle(page)
            [gate_session] = _gate_sessions(headed)
            gate_session.replies["Target.getTargetInfo"] = {
                "targetInfo": {"targetId": "shadow-page", "type": "page", "browserContextId": headed.context.context_id}
            }
            gate_session.emit(
                "Fetch.requestPaused",
                {
                    "requestId": "shadow-refresh",
                    "frameId": "shadow-page",
                    "request": {"url": "https://refused.example/secret", "method": "GET", "headers": {}},
                },
            )
            await gate_session.wait_for_reply("shadow-refresh")

        self.manager._settle = settles_then_leaves  # type: ignore[method-assign]
        self.manager.request_gates.target_id_of = AsyncMock(return_value="shadow-page")  # type: ignore[method-assign]

        with self.assertRaises(BrowserActionError) as caught:
            await self.manager.enable_shadow_browse(source_id)

        self.assertEqual(
            (caught.exception.code, caught.exception.action), ("browser_action_blocked", "enable_shadow_browse")
        )
        headed.close.assert_awaited_once()

    # ── round 4: F12, guards no test held ────────────────────────────────────

    async def test_f12b_a_reconnect_releases_the_disconnected_browsers_gate(self) -> None:
        self.manager.settings.browser_ws_endpoint = "ws://browser-node:9223/x"
        fresh = _browser()
        self._playwright(connect=AsyncMock(side_effect=[self.browser, fresh]))
        await self.manager.ensure_browser()
        self.browser.is_connected = lambda: False  # type: ignore[attr-defined]  # browser-node restarted
        self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
        await self._settled()

        await self.manager.ensure_browser()

        self.assertIsNone(self.manager.request_gates.get(self.browser))
        self.assertEqual([gate["state"] for gate in self.manager.request_gates.status()["gates"]], ["attached"])

    async def test_f12b_a_failed_isolated_create_leaves_no_gate_entry(self) -> None:
        self._isolated()
        self.browser.context.page.goto = AsyncMock(side_effect=PlaywrightError("Page.goto: Timeout 30000ms exceeded."))

        with self.assertRaises(PlaywrightError):
            await self.manager.create_session(name="s", start_url=START_URL)

        self.assertIsNone(self.manager.request_gates.get(self.browser))
        self.assertEqual(self.manager.request_gates.status()["gates"], [])

    async def test_f12e_a_start_url_that_fails_for_another_reason_keeps_its_own_error(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        self.browser.context.page.goto = AsyncMock(
            side_effect=PlaywrightError("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://example.com/")
        )

        with self.assertRaises(PlaywrightError):
            await self.manager.create_session(name="s", start_url=START_URL)

    async def test_f12e_a_shadow_page_that_fails_for_another_reason_keeps_its_own_error(self) -> None:
        self.manager.settings.shadow_browse_enabled = True
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        source_id = (await self.manager.create_session(name="source", start_url=START_URL))["id"]
        self.manager.sessions[source_id].context.storage_state = AsyncMock(return_value={})
        headed = _browser()
        headed.context.page.goto = AsyncMock(side_effect=PlaywrightError("Page.goto: Timeout 30000ms exceeded."))
        self._playwright(launch=AsyncMock(return_value=headed))

        with self.assertRaises(PlaywrightError):
            await self.manager.enable_shadow_browse(source_id)

    async def _run(self, session, operation, *, target_id: str | None = "the-page", on_snapshot=None) -> dict:
        """The action pipeline over the fake browser, its observations stubbed (``on_snapshot`` runs in the
        action's before-snapshot)."""
        from app.browser_manager import BrowserManager as Manager

        before = {"url": START_URL, "title": "Before"}
        allowed = SimpleNamespace(should_block=False, block_reason=None, require_approval=False)
        self.manager.witness_policy.evaluate_action = Mock(return_value=allowed)  # type: ignore[method-assign]

        async def snapshot(*_args, **_kwargs) -> dict:
            if on_snapshot is not None:
                on_snapshot()
            return before

        self.manager._light_snapshot = AsyncMock(side_effect=snapshot)  # type: ignore[method-assign]
        self.manager._observation_payload = AsyncMock(return_value=before)  # type: ignore[method-assign]
        self.manager._ensure_witness_remote_ready = AsyncMock()  # type: ignore[method-assign]
        self.manager._check_bot_challenge = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self.manager._maybe_handle_totp = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self.manager.request_gates.target_id_of = AsyncMock(return_value=target_id)  # type: ignore[method-assign]
        return await Manager._run_action(self.manager, session, "click", {"selector": "#go"}, operation)

    def _stop(self, session, request_id: str) -> None:
        [gate_session] = _gate_sessions(self.browser)
        gate_session.replies["Target.getTargetInfo"] = {
            "targetInfo": {"targetId": "the-page", "type": "page", "browserContextId": session.gate_context_id}
        }
        gate_session.emit(
            "Fetch.requestPaused",
            {
                "requestId": request_id,
                "frameId": "the-page",
                "request": {"url": "https://refused.example/secret", "method": "GET", "headers": {}},
            },
        )

    async def test_f12e_an_action_reports_only_the_blocks_it_led_to(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        session = self.manager.sessions[(await self.manager.create_session(name="s", start_url=START_URL))["id"]]
        self._stop(session, "earlier")
        await _gate_sessions(self.browser)[0].wait_for_reply("earlier")

        quiet = await self._run(session, AsyncMock())

        async def leads_to_a_block() -> None:
            self._stop(session, "this-one")
            await _gate_sessions(self.browser)[0].wait_for_reply("this-one")

        stopped = await self._run(session, leads_to_a_block)

        self.assertNotIn("blocked_navigations", quiet["target"], "an earlier block was reported as this action's")
        self.assertEqual([b["origin"] for b in stopped["target"]["blocked_navigations"]], ["https://refused.example"])

    async def test_f9b_a_stop_the_pipeline_reports_names_the_refused_origin_in_url(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        session = self.manager.sessions[(await self.manager.create_session(name="s", start_url=START_URL))["id"]]

        async def stopped() -> None:
            self._stop(session, "nav")
            await _gate_sessions(self.browser)[0].wait_for_reply("nav")
            raise PlaywrightError("Page.click: net::ERR_BLOCKED_BY_CLIENT at https://refused.example/secret")

        self.manager.action_pipeline._failure_snapshot = AsyncMock(return_value={})  # type: ignore[method-assign]
        with self.assertRaises(BrowserActionError) as caught:
            await self._run(session, stopped)

        self.assertEqual(
            (caught.exception.code, caught.exception.url), ("browser_action_blocked", "https://refused.example")
        )

    # ── round 4: F2 and F4 through the pipeline and the controller's own navigations ──

    async def _gated_session(self):
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        return self.manager.sessions[(await self.manager.create_session(name="s", start_url=START_URL))["id"]]

    def _lose_the_gate(self) -> None:
        self.manager.runtime.gate_lost = lambda _gate: None  # type: ignore[method-assign]  # containment not yet run
        [gate_session] = _gate_sessions(self.browser)
        gate_session.emit("close", gate_session)

    async def test_f2_an_action_on_a_lost_gate_takes_no_snapshot_and_does_not_run(self) -> None:
        session = await self._gated_session()
        self._lose_the_gate()
        operation = AsyncMock()

        with self.assertRaises(SessionNotFoundError) as caught:
            await self._run(session, operation)

        self.assertEqual(caught.exception.reason, "request_gate_lost")
        self.manager._light_snapshot.assert_not_awaited()
        operation.assert_not_awaited()

    async def test_f2_a_gate_lost_while_the_action_prepares_stops_it_before_the_operation(self) -> None:
        session = await self._gated_session()
        operation = AsyncMock()

        with self.assertRaises(SessionNotFoundError) as caught:
            await self._run(session, operation, on_snapshot=self._lose_the_gate)

        self.assertEqual(caught.exception.reason, "request_gate_lost")
        self.manager._light_snapshot.assert_awaited()
        operation.assert_not_awaited()

    async def test_f2_a_start_url_on_a_gate_lost_while_the_session_starts_is_not_sent(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        self.manager.runtime.gate_lost = lambda _gate: None  # type: ignore[method-assign]

        async def bypass_then_lose(_page) -> None:
            [gate_session] = _gate_sessions(self.browser)
            gate_session.emit("close", gate_session)

        self.manager.request_gates.bypass_service_workers = bypass_then_lose  # type: ignore[method-assign]
        page = self.browser.context.page
        page.goto = AsyncMock()

        with self.assertRaises(RequestGateUnavailable):
            await self.manager.create_session(name="s", start_url=START_URL)

        page.goto.assert_not_awaited()

    async def test_f4_a_pipeline_stop_with_no_attributable_block_is_a_block_naming_no_url(self) -> None:
        session = await self._gated_session()

        async def stopped() -> None:
            raise PlaywrightError("Page.click: net::ERR_BLOCKED_BY_CLIENT at https://refused.example/secret")

        self.manager.action_pipeline._failure_snapshot = AsyncMock(return_value={})  # type: ignore[method-assign]
        with self.assertRaises(BrowserActionError) as caught:
            await self._run(session, stopped, target_id=None)

        self.assertEqual(
            (caught.exception.code, caught.exception.message, caught.exception.url),
            ("browser_action_blocked", "A navigation was stopped before it was sent by the pre-request gate.", None),
        )

    async def test_f4_a_start_url_stop_with_no_attributable_block_is_a_block_naming_no_url(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        self.browser.context.page.goto = AsyncMock(
            side_effect=PlaywrightError("Page.goto: net::ERR_BLOCKED_BY_CLIENT at https://example.com/")
        )

        with self.assertRaises(BrowserActionError) as caught:
            await self.manager.create_session(name="s", start_url=START_URL)

        self.assertEqual(
            (caught.exception.code, caught.exception.message, caught.exception.url),
            ("browser_action_blocked", "A navigation was stopped before it was sent by the pre-request gate.", None),
        )

    # ── round 5 ──────────────────────────────────────────────────────────────

    async def test_s1_a_shared_browser_whose_disconnect_fails_keeps_its_survivors_gated(self) -> None:
        self.manager.settings.browser_ws_endpoint = "ws://browser-node:9223/x"
        self._playwright(connect=AsyncMock(return_value=self.browser))
        self.browser.close = AsyncMock(side_effect=RuntimeError("disconnect failed"))  # type: ignore[attr-defined]
        survivor_id = (await self.manager.create_session(name="one"))["id"]
        survivor = self.manager.sessions[survivor_id]
        survivor.context.close = AsyncMock(side_effect=RuntimeError("context close failed"))

        with self.assertLogs("app.browser.services.runtime", level="ERROR") as logs:
            self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
            await self._settled()

        self.browser.close.assert_awaited_once()
        gate = self.manager.request_gates.get(self.browser)
        self.assertTrue(gate is not None and gate.live, "the replacement gate was released before the disconnect")
        self.assertTrue(gate.owns(survivor.gate_context_id), "the survivor's context is no longer gated")
        self.assertIn("disconnecting the shared browser failed", " ".join(logs.output))
        replacement = _gate_sessions(self.browser)[-1]
        replacement.replies["Target.getTargetInfo"] = {
            "targetInfo": {"targetId": "left", "type": "page", "browserContextId": survivor.gate_context_id}
        }
        replacement.emit(
            "Fetch.requestPaused",
            {"requestId": "r1", "frameId": "left", "request": {"url": "https://refused.example/", "headers": {}}},
        )
        method, _ = await replacement.wait_for_reply("r1")
        self.assertEqual(method, "Fetch.failRequest")

    async def test_s2_a_shadow_rollback_whose_context_close_never_settles_still_returns_and_closes(self) -> None:
        from app.browser.services import sessions as sessions_module

        self.manager.settings.shadow_browse_enabled = True
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        source_id = (await self.manager.create_session(name="source", start_url=START_URL))["id"]
        self.manager.sessions[source_id].context.storage_state = AsyncMock(return_value={})
        headed = _browser()
        headed.context.page.goto = AsyncMock(side_effect=RuntimeError("shadow goto failed"))

        async def never() -> None:
            await asyncio.Event().wait()

        headed.context.close = AsyncMock(side_effect=never)
        self._playwright(launch=AsyncMock(return_value=headed))

        with patch.object(sessions_module, "TEARDOWN_STEP_TIMEOUT_SECONDS", 0.05):
            with self.assertRaisesRegex(RuntimeError, "shadow goto failed"):
                await asyncio.wait_for(self.manager.enable_shadow_browse(source_id), timeout=5.0)

        headed.close.assert_awaited_once()

    async def test_s3_a_gate_lost_while_a_tabs_bypass_is_set_stops_it_before_goto(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        session_id = (await self.manager.create_session(name="s"))["id"]
        context = self.manager.sessions[session_id].context
        tab = FakePage()
        tab.context = context
        tab.goto = AsyncMock()
        context.new_page = AsyncMock(return_value=tab)
        self.manager.runtime.gate_lost = lambda _gate: None  # type: ignore[method-assign]  # containment not yet run

        async def bypass_then_lose(_page) -> None:
            [gate_session] = _gate_sessions(self.browser)
            gate_session.emit("close", gate_session)

        self.manager.request_gates.bypass_service_workers = bypass_then_lose  # type: ignore[method-assign]

        with self.assertRaises(SessionNotFoundError) as caught:
            await self.manager.open_tab(session_id, "https://example.com/tab", True)

        self.assertEqual(caught.exception.reason, "request_gate_lost")
        tab.goto.assert_not_awaited()

    async def test_p2_a_new_tab_that_leaves_for_a_refused_host_while_it_settles_is_a_block(self) -> None:
        """Codex P2 on #240: goto returned, then the page headed for a refused host during the settle."""
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        session_id = (await self.manager.create_session(name="s"))["id"]
        session = self.manager.sessions[session_id]
        first_page = session.page
        tab = FakePage()
        tab.context = session.context
        tab.close = AsyncMock()  # FakePage's goto reaches domcontentloaded on the allowed URL
        session.context.new_page = AsyncMock(return_value=tab)
        self.manager.request_gates.target_id_of = AsyncMock(return_value="tab-2")  # type: ignore[method-assign]
        settle = self.manager._settle

        async def settles_then_leaves(page) -> None:
            if page is not tab:
                return await settle(page)
            [gate_session] = _gate_sessions(self.browser)
            gate_session.replies["Target.getTargetInfo"] = {
                "targetInfo": {"targetId": "tab-2", "type": "page", "browserContextId": session.gate_context_id}
            }
            gate_session.emit(
                "Fetch.requestPaused",
                {
                    "requestId": "tab-refresh",
                    "frameId": "tab-2",
                    "request": {"url": "https://refused.example/secret", "method": "GET", "headers": {}},
                },
            )
            await gate_session.wait_for_reply("tab-refresh")
            tab.url = "chrome-error://chromewebdata/"

        self.manager._settle = settles_then_leaves  # type: ignore[method-assign]

        with self.assertRaises(BrowserActionError) as caught:
            await self.manager.open_tab(session_id, "https://example.com/tab", True)

        self.assertEqual(
            (caught.exception.code, caught.exception.action, caught.exception.url),
            ("browser_action_blocked", "open_tab", "https://refused.example"),
        )
        tab.close.assert_awaited_once()
        self.assertIs(session.page, first_page, "the refused tab was activated")


if __name__ == "__main__":
    unittest.main()
