""""Remember me": Open loads a default profile automatically, Close (and a
periodic tick while open) save into it automatically -- with no button.

Root cause this covers: `create_session` only ever loaded cookies when the
caller *named* an `auth_profile`; an owner who never types a profile name
into the optional field always got a stark-fresh browser, even though the
profile data itself survives container recreation and reboots fine (it lives
on the tenant's persistent `/data` volume). See auto_persist_* settings in
app/config.py and the auto-persist wiring in
app/browser/services/sessions.py + app/browser/services/auth_profiles.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.browser_manager import BrowserManager
from app.config import Settings


def _settings(root: Path, **overrides) -> Settings:
    kwargs = dict(
        ARTIFACT_ROOT=str(root / "artifacts"),
        UPLOAD_ROOT=str(root / "uploads"),
        AUTH_ROOT=str(root / "auth"),
        APPROVAL_ROOT=str(root / "approvals"),
        AUDIT_ROOT=str(root / "audit"),
        WITNESS_ROOT=str(root / "witness"),
        SESSION_STORE_ROOT=str(root / "sessions"),
        REMOTE_ACCESS_INFO_PATH=str(root / "tunnels/reverse-ssh.json"),
        BROWSER_WS_ENDPOINT_FILE=str(root / "missing-ws.txt"),
        WITNESS_ENABLED=False,
        NETWORK_INSPECTOR_ENABLED=False,
        ENABLE_TRACING=False,
        STEALTH_ENABLED=False,
        MAX_SESSIONS=2,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.on = Mock()
        self.set_default_timeout = Mock()
        self.goto = AsyncMock(side_effect=self._goto)
        self.title = AsyncMock(return_value="Fixture Page")

    async def _goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        self.url = url


class FakeContext:
    """Stands in for a Playwright BrowserContext, including a real-ish
    storage_state(path=...) that actually writes a file -- AuthStateManager
    relies on the file existing afterwards, not on the return value.
    """

    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.pages = [page]
        self.on = Mock()
        self.close = AsyncMock()
        self.new_page = AsyncMock(return_value=page)
        self.saved_paths: list[str] = []

    async def storage_state(self, path: str | None = None) -> dict:
        state = {"cookies": [{"name": "session", "value": "logged-in"}], "origins": []}
        if path:
            self.saved_paths.append(path)
            Path(path).write_text(json.dumps(state), encoding="utf-8")
        return state


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.new_context = AsyncMock(return_value=context)


class AutoPersistLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def _manager(self, **settings_overrides) -> BrowserManager:
        manager = BrowserManager(_settings(self.root, **settings_overrides))
        manager.audit.append = AsyncMock()
        manager._persist_session = AsyncMock()
        manager._settle = AsyncMock()
        manager._maybe_provision_session_tunnel = AsyncMock()
        return manager

    @staticmethod
    async def _cancel(session) -> None:
        if session.auto_persist_task is None:
            return
        session.auto_persist_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await session.auto_persist_task

    def _wire_browser(self, manager: BrowserManager) -> tuple[FakeBrowser, FakeContext, FakePage]:
        page = FakePage()
        context = FakeContext(page)
        browser = FakeBrowser(context)
        manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]
        return browser, context, page

    async def test_open_with_no_profile_named_auto_loads_the_default_profile(self) -> None:
        """The core bug: leaving the profile field blank must not mean
        'fresh browser' once a remembered login exists."""
        manager = self._manager()
        # Seed the default profile as if an earlier session had already
        # auto-saved into it.
        await manager.auth_profiles.save_for_session(
            SimpleNamespace(
                id="earlier",
                context=FakeContext(FakePage()),
                page=FakePage(),
            ),
            "owner-default",
        )
        browser, context, page = self._wire_browser(manager)

        result = await manager.create_session(name="reopened")

        session = manager.sessions[result["id"]]
        # No profile was named, yet the context was built with the
        # remembered storage state, and the session knows where it came from.
        context_kwargs = browser.new_context.await_args.kwargs
        self.assertIn("storage_state", context_kwargs)
        self.assertIsNotNone(session.last_auth_state_path)
        # auth_profile_name (the *named* profile) stays unset -- auto-loading
        # the default must not make later manual saves silently target it
        # under a different name than the caller expects.
        self.assertIsNone(session.auth_profile_name)
        self.assertEqual(session.auto_persist_profile_name, "owner-default")

        await self._cancel(session)

    async def test_open_with_no_default_profile_yet_opens_fresh_without_error(self) -> None:
        manager = self._manager()
        browser, context, page = self._wire_browser(manager)

        result = await manager.create_session(name="first-ever")

        session = manager.sessions[result["id"]]
        context_kwargs = browser.new_context.await_args.kwargs
        self.assertNotIn("storage_state", context_kwargs)
        self.assertIsNone(session.last_auth_state_path)

        await self._cancel(session)

    async def test_explicit_named_profile_auto_persists_into_itself_not_the_default(self) -> None:
        """Regression: a session opened from a named profile ('nihad-google')
        must keep that profile's cookies fresh, not silently refresh
        'owner-default' instead -- that mismatch is exactly what let
        'nihad-google' go stale after 2026-09-23 while 'owner-default' kept
        getting rewritten underneath it."""
        manager = self._manager()
        await manager.auth_profiles.save_for_session(
            SimpleNamespace(id="earlier", context=FakeContext(FakePage()), page=FakePage()),
            "nihad-google",
        )
        browser, context, page = self._wire_browser(manager)

        result = await manager.create_session(name="named", auth_profile="nihad-google")

        session = manager.sessions[result["id"]]
        self.assertEqual(session.auth_profile_name, "nihad-google")
        self.assertEqual(session.auto_persist_profile_name, "nihad-google")

        await self._cancel(session)

    async def test_close_saves_into_the_default_profile_without_a_button(self) -> None:
        manager = self._manager()
        browser, context, page = self._wire_browser(manager)
        result = await manager.create_session(name="to-close")
        session_id = result["id"]

        await manager.close_session(session_id)

        state_path = Path(manager.settings.auth_root) / "profiles" / "owner-default" / "state.json"
        self.assertTrue(state_path.exists(), "close() must auto-save the default profile")
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["cookies"][0]["value"], "logged-in")

    async def test_close_cancels_the_periodic_autosave_task(self) -> None:
        manager = self._manager()
        self._wire_browser(manager)
        result = await manager.create_session(name="to-close-2")
        session = manager.sessions[result["id"]]
        task = session.auto_persist_task
        self.assertIsNotNone(task)

        await manager.close_session(result["id"])

        self.assertTrue(task.done())
        self.assertIsNone(session.auto_persist_task)

    async def test_periodic_tick_refreshes_the_default_profile_while_open(self) -> None:
        # A tiny real interval so the background loop actually ticks during
        # the test, instead of only proving anything at Close.
        manager = self._manager(AUTO_PERSIST_INTERVAL_SECONDS=0.01)
        self._wire_browser(manager)
        save_spy = AsyncMock(wraps=manager.auth_profiles.save_auto_persist)
        manager.auth_profiles.save_auto_persist = save_spy  # type: ignore[method-assign]

        result = await manager.create_session(name="long-lived")
        session = manager.sessions[result["id"]]

        # Give the loop a few ticks' worth of wall-clock time.
        for _ in range(20):
            if save_spy.await_count:
                break
            await asyncio.sleep(0.02)

        self.assertGreaterEqual(save_spy.await_count, 1)
        save_spy.assert_awaited_with(session, "owner-default")

        session.auto_persist_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await session.auto_persist_task

    async def test_auto_persist_disabled_never_loads_or_saves(self) -> None:
        manager = self._manager(AUTO_PERSIST_LOGIN_ENABLED=False)
        await manager.auth_profiles.save_for_session(
            SimpleNamespace(id="earlier", context=FakeContext(FakePage()), page=FakePage()),
            "owner-default",
        )
        browser, context, page = self._wire_browser(manager)

        result = await manager.create_session(name="disabled")
        session = manager.sessions[result["id"]]
        context_kwargs = browser.new_context.await_args.kwargs
        self.assertNotIn("storage_state", context_kwargs)
        self.assertIsNone(session.auto_persist_task)

        await manager.close_session(result["id"])
        # No new save should have happened (only the seed write above).
        self.assertEqual(len(context.saved_paths), 0)


if __name__ == "__main__":
    unittest.main()
