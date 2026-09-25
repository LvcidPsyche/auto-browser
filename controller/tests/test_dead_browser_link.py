"""A dead browser link (Playwright driver exit / CDP drop) never leaves a zombie.

2026-09-25: the controller's Playwright driver died on an unhandled assertion
while the owner's persistent-profile session was open. Every later call failed
with "... the handler is closed", GET /sessions answered 500, the session
stayed "active", and the portal's Connect spun until someone restarted the
controller. These tests pin the recovery: detect, restart the driver, re-attach
a persistent-profile session in place (same id, still-running Chromium), and
retire anything that cannot be re-attached -- without closing the owner's
profile in browser-node.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.browser.services.connection_health import (
    is_driver_dead_error,
    playwright_driver_alive,
    session_connection_problem,
)
from app.browser.services.storage_capture import TablessStorageState, storage_state_source
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.persistent_profiles import PersistentProfileHandle
from app.utils import UTC


def _fake_playwright(*, dead: bool) -> SimpleNamespace:
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    if dead:
        future.set_result(None)
    transport = SimpleNamespace(on_error_future=future)
    connection = SimpleNamespace(_closed_error=None, _transport=transport)
    return SimpleNamespace(_impl_obj=SimpleNamespace(_connection=connection), stop=AsyncMock())


class FakeBrowser:
    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.close = AsyncMock()

    def is_connected(self) -> bool:
        return self.connected


class FakePage:
    def __init__(self, url: str = "https://accounts.google.com/") -> None:
        self.url = url
        self.set_default_timeout = MagicMock()
        self.on = MagicMock()
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def title(self) -> str:
        return "Google"


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        ARTIFACT_ROOT=str(root / "artifacts"),
        UPLOAD_ROOT=str(root / "uploads"),
        AUTH_ROOT=str(root / "auth"),
        APPROVAL_ROOT=str(root / "approvals"),
        AUDIT_ROOT=str(root / "audit"),
        WITNESS_ROOT=str(root / "witness"),
        SESSION_STORE_ROOT=str(root / "sessions"),
        MAX_SESSIONS=1,
        AUTO_PERSIST_INTERVAL_SECONDS=0,
        SESSION_WATCHDOG_INTERVAL_SECONDS=0,
    )


class ConnectionHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_driver_dead_errors_are_recognised_through_the_cause_chain(self) -> None:
        self.assertTrue(is_driver_dead_error(Exception("BrowserContext.storage_state: Connection closed while reading from the driver")))
        self.assertTrue(
            is_driver_dead_error(
                RuntimeError(
                    "Page.title: unable to perform operation on <WriteUnixTransport closed=True "
                    "reading=False 0x7>; the handler is closed"
                )
            )
        )
        try:
            try:
                raise RuntimeError("unable to perform operation on <X>; the handler is closed")
            except RuntimeError as inner:
                raise ValueError("wrapped") from inner
        except ValueError as outer:
            self.assertTrue(is_driver_dead_error(outer))
        self.assertFalse(is_driver_dead_error(Exception("Target page, context or browser has been closed")))
        self.assertFalse(is_driver_dead_error(None))

    async def test_driver_alive_reads_the_client_transport_state(self) -> None:
        self.assertTrue(playwright_driver_alive(_fake_playwright(dead=False)))
        self.assertFalse(playwright_driver_alive(_fake_playwright(dead=True)))
        closed = _fake_playwright(dead=False)
        closed._impl_obj._connection._closed_error = RuntimeError("closed")
        self.assertFalse(playwright_driver_alive(closed))
        # Unknown shapes (test doubles, other versions) are never "dead".
        self.assertTrue(playwright_driver_alive(MagicMock()))
        self.assertTrue(playwright_driver_alive(None))

    async def test_session_problem_covers_driver_epoch_and_cdp_drop(self) -> None:
        manager = SimpleNamespace(playwright=_fake_playwright(dead=False), _driver_epoch=2)
        live = SimpleNamespace(browser=FakeBrowser(), driver_epoch=2)
        self.assertIsNone(session_connection_problem(manager, live))
        stale = SimpleNamespace(browser=FakeBrowser(), driver_epoch=1)
        self.assertIn("driver", session_connection_problem(manager, stale))
        dropped = SimpleNamespace(browser=FakeBrowser(connected=False), driver_epoch=2)
        self.assertIn("browser connection closed", session_connection_problem(manager, dropped))
        manager.playwright = _fake_playwright(dead=True)
        self.assertIn("driver", session_connection_problem(manager, live))


class DeadLinkRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.manager = BrowserManager(_settings(self.root))
        self.manager.session_store.list = AsyncMock(return_value=[])  # type: ignore[method-assign]
        self.manager.session_store.upsert = AsyncMock()  # type: ignore[method-assign]
        self.manager.audit.append = AsyncMock()  # type: ignore[method-assign]
        self.manager.persistent_profiles.close = AsyncMock(return_value=True)  # type: ignore[method-assign]
        self.dead_driver = _fake_playwright(dead=True)
        self.new_driver = _fake_playwright(dead=False)
        self.manager.playwright = self.dead_driver  # type: ignore[assignment]
        starter = MagicMock()
        starter.start = AsyncMock(return_value=self.new_driver)
        patcher = patch("app.browser_manager.async_playwright", return_value=starter)
        self.async_playwright = patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    def _session(self, *, persistent: bool, browser: FakeBrowser | None = None) -> BrowserSession:
        sid = "s-persistent" if persistent else "s-fresh"
        artifact_dir = self.root / "artifacts" / sid
        artifact_dir.mkdir(parents=True, exist_ok=True)
        context = AsyncMock()
        context.pages = []
        session = BrowserSession(
            id=sid,
            name=sid,
            created_at=datetime.now(UTC),
            context=context,
            page=FakePage(),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=self.root / "auth" / sid,
            upload_dir=self.root / "uploads" / sid,
            takeover_url=self.manager.settings.takeover_url,
            trace_path=artifact_dir / "trace.zip",
            browser=browser or FakeBrowser(),  # type: ignore[arg-type]
            persistent_profile_name="owner-default" if persistent else None,
            persistent_profile_generation="gen-1" if persistent else None,
            persistent_open_options=(
                {"owner": "tenant", "adopt_unmarked": True, "context_kwargs": {"locale": "ar-EG"}}
                if persistent
                else None
            ),
        )
        self.manager.sessions[session.id] = session
        return session

    def _stub_reattach(self) -> tuple[FakeBrowser, FakePage]:
        new_browser = FakeBrowser()
        new_page = FakePage("https://mail.google.com/")
        new_context = MagicMock()
        new_context.pages = [new_page]
        self.manager.persistent_profiles.open = AsyncMock(  # type: ignore[method-assign]
            return_value=PersistentProfileHandle(
                name="owner-default",
                cdp_endpoint="ws://browser-node:9225/cdp/owner-default/devtools/browser/x",
                already_open=True,
                seeded=False,
                was_empty=False,
                generation="gen-2",
            )
        )
        self.manager.runtime.attach_persistent_context = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(browser=new_browser, context=new_context, handle=None)
        )
        return new_browser, new_page

    async def test_dead_driver_persistent_session_is_reattached_in_place(self) -> None:
        session = self._session(persistent=True)
        old_browser = session.browser
        new_browser, new_page = self._stub_reattach()

        listed = await self.manager.list_sessions()

        # A new driver was started, and the session kept its id and is live
        # on the SAME running profile (already_open), under a new lease.
        self.assertIs(self.manager.playwright, self.new_driver)
        self.assertEqual(self.manager._driver_epoch, 1)
        self.assertIs(self.manager.sessions["s-persistent"], session)
        self.assertIs(session.browser, new_browser)
        self.assertIs(session.page, new_page)
        self.assertEqual(session.persistent_profile_generation, "gen-2")
        self.assertEqual(session.driver_epoch, 1)
        self.assertEqual(session.reattach_count, 1)
        self.assertFalse(session.persistent_profile_released)
        self.manager.persistent_profiles.open.assert_awaited_once_with(
            "owner-default",
            owner="tenant",
            adopt_unmarked=True,
            context_kwargs={"locale": "ar-EG"},
            reattach_only=True,
        )
        # The owner's profile was never closed in browser-node.
        self.manager.persistent_profiles.close.assert_not_awaited()
        old_browser.close.assert_awaited()
        self.assertEqual([item["status"] for item in listed if item["id"] == "s-persistent"], ["active"])
        events = [call.kwargs.get("event_type") for call in self.manager.audit.append.await_args_list]
        self.assertIn("playwright_driver_restarted", events)
        self.assertIn("session_reattached", events)

    async def test_failed_reattach_retires_the_session_but_keeps_the_profile_running(self) -> None:
        session = self._session(persistent=True)
        self.manager.persistent_profiles.open = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("browser-node unreachable")
        )

        listed = await self.manager.list_sessions()

        self.assertNotIn(session.id, self.manager.sessions)
        self.assertTrue(session.persistent_profile_released)
        # keep_profile_open: no /profiles/close -- the next Open re-attaches.
        self.manager.persistent_profiles.close.assert_not_awaited()
        self.assertFalse(any(item.get("status") == "active" for item in listed))
        retired = self.manager.session_store.upsert.await_args.args[0]
        self.assertEqual(retired.status, "interrupted")
        # The session slot is free again: the next Open is allowed.
        self.manager._check_session_limit()

    async def test_dead_driver_fresh_context_session_is_retired(self) -> None:
        session = self._session(persistent=False)
        await self.manager.list_sessions()
        self.assertNotIn(session.id, self.manager.sessions)
        self.assertIs(self.manager.playwright, self.new_driver)

    async def test_cdp_drop_reattaches_without_restarting_a_live_driver(self) -> None:
        self.manager.playwright = self.new_driver  # type: ignore[assignment]
        session = self._session(persistent=True, browser=FakeBrowser(connected=False))
        new_browser, _page = self._stub_reattach()

        await self.manager.session_lifecycle.reap_dead_sessions()

        self.async_playwright.assert_not_called()
        self.assertIs(session.browser, new_browser)
        self.assertEqual(session.reattach_count, 1)

    async def test_driver_dead_with_no_sessions_still_gets_a_new_driver(self) -> None:
        await self.manager.session_lifecycle.reap_dead_sessions()
        self.assertIs(self.manager.playwright, self.new_driver)

    async def test_healthy_sessions_are_left_alone(self) -> None:
        self.manager.playwright = self.new_driver  # type: ignore[assignment]
        session = self._session(persistent=True)
        self.manager.persistent_profiles.open = AsyncMock()  # type: ignore[method-assign]
        await self.manager.list_sessions()
        self.assertIs(self.manager.sessions[session.id], session)
        self.manager.persistent_profiles.open.assert_not_awaited()
        self.async_playwright.assert_not_called()

    async def test_auto_persist_failure_on_a_dead_driver_triggers_recovery(self) -> None:
        session = self._session(persistent=True)
        self._stub_reattach()
        calls = 0

        async def save(_session, _name):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError(
                    "BrowserContext.storage_state: unable to perform operation on "
                    "<WriteUnixTransport closed=True>; the handler is closed"
                )
            raise asyncio.CancelledError  # stop the loop after the recovery pass

        self.manager.auth_profiles.save_auto_persist = save  # type: ignore[method-assign]
        with self.assertRaises(asyncio.CancelledError):
            await self.manager.session_lifecycle._auto_persist_loop(session, "owner-default")
        self.assertEqual(calls, 2, "the loop kept running on the re-attached session")
        self.assertEqual(session.reattach_count, 1)
        self.assertIn(session.id, self.manager.sessions)

    async def test_concurrent_reaps_share_one_recovery(self) -> None:
        self._session(persistent=True)
        self._stub_reattach()
        await asyncio.gather(*(self.manager.session_lifecycle.reap_dead_sessions() for _ in range(5)))
        self.assertEqual(self.async_playwright.call_count, 1)
        self.manager.persistent_profiles.open.assert_awaited_once()

    async def test_a_hung_observe_answers_504_frees_the_lock_and_reattaches(self) -> None:
        """2026-09-25 04:01: an employee's observe never returned, held the
        session lock, and every later call queued behind it forever."""
        from app.action_errors import BrowserActionError

        self.manager.playwright = self.new_driver  # type: ignore[assignment]
        self.manager.settings.browser_call_timeout_seconds = 0.2
        session = self._session(persistent=True)
        new_browser, new_page = self._stub_reattach()

        async def never(*_args, **_kwargs):
            await asyncio.Event().wait()

        self.manager.observation.observation_payload = never  # type: ignore[method-assign]
        with self.assertRaises(BrowserActionError) as raised:
            await self.manager.observe(session.id)
        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(raised.exception.payload["code"], "browser_call_timeout")
        self.assertFalse(session.lock.locked(), "the lock is released at once")
        for _ in range(50):
            if session.reattach_count:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(session.reattach_count, 1)
        self.assertIs(session.browser, new_browser)
        self.assertIs(session.page, new_page)
        self.assertIsNone(session.unresponsive_reason)
        self.manager.persistent_profiles.close.assert_not_awaited()

    async def test_a_hung_action_answers_504_instead_of_holding_the_lock(self) -> None:
        from app.action_errors import BrowserActionError
        from app.actions.pipeline import ActionRunContext

        self.manager.playwright = self.new_driver  # type: ignore[assignment]
        self.manager.settings.browser_action_timeout_seconds = 0.2
        session = self._session(persistent=True)
        self._stub_reattach()

        async def never() -> None:
            await asyncio.Event().wait()

        pipeline = self.manager.action_pipeline
        pipeline._prepare = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        context = ActionRunContext(manager=self.manager, session=session, action_name="click", target={}, operation=never)
        async def execute_never(*_args) -> None:
            await never()

        pipeline._execute = execute_never  # type: ignore[method-assign]
        with self.assertRaises(BrowserActionError) as raised:
            await pipeline.run(context)
        self.assertEqual(raised.exception.status_code, 504)
        self.assertFalse(session.lock.locked())


class TablessStorageStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_cookies_and_open_tabs_without_creating_a_page(self) -> None:
        good = MagicMock()
        good.is_closed.return_value = False
        good.evaluate = AsyncMock(
            return_value={"origin": "https://mail.google.com", "localStorage": [{"name": "k", "value": "v"}]}
        )
        empty = MagicMock()
        empty.is_closed.return_value = False
        empty.evaluate = AsyncMock(return_value={"origin": "https://x.test", "localStorage": []})
        stuck = MagicMock()
        stuck.is_closed.return_value = False
        stuck.evaluate = AsyncMock(side_effect=RuntimeError("Execution context was destroyed"))
        context = MagicMock()
        context.cookies = AsyncMock(return_value=[{"name": "SID", "domain": ".google.com"}])
        context.pages = [good, empty, stuck]
        context.new_page = AsyncMock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = await TablessStorageState(context).storage_state(path=str(path))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), state)
        self.assertEqual(state["cookies"], [{"name": "SID", "domain": ".google.com"}])
        self.assertEqual(state["origins"], [{"origin": "https://mail.google.com", "localStorage": [{"name": "k", "value": "v"}]}])
        context.new_page.assert_not_called()

    async def test_only_persistent_sessions_use_the_tabless_reader(self) -> None:
        context = MagicMock()
        self.assertIsInstance(
            storage_state_source(SimpleNamespace(persistent_profile_name="owner-default", context=context)),
            TablessStorageState,
        )
        self.assertIs(storage_state_source(SimpleNamespace(persistent_profile_name=None, context=context)), context)


class DriverDeadHttpErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_dead_driver_error_answers_503_and_starts_recovery(self) -> None:
        from app import main as main_module

        with patch.object(
            main_module.manager.session_lifecycle, "reap_dead_sessions", AsyncMock()
        ) as reap:
            response = await main_module.handle_unexpected_error(
                None,  # type: ignore[arg-type]
                RuntimeError("Page.title: unable to perform operation on <X>; the handler is closed"),
            )
            await asyncio.sleep(0)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(json.loads(response.body)["code"], "browser_connection_lost")
            reap.assert_awaited_once()
            other = await main_module.handle_unexpected_error(None, ValueError("boom"))  # type: ignore[arg-type]
            self.assertEqual(other.status_code, 500)


if __name__ == "__main__":
    unittest.main()
