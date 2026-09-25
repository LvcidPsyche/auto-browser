from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.audit import reset_current_operator, set_current_operator
from app.browser.services.runtime import PersistentProfileAttachment
from app.browser_manager import BrowserManager
from app.config import Settings
from app.persistent_profiles import PersistentProfileError, PersistentProfileHandle


def _settings(root: Path, **overrides) -> Settings:
    kwargs = dict(
        _env_file=None,
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
        PERSISTENT_PROFILES_ENABLED=True,
        PROFILE_CONTROL_TOKEN="test-token",
        SESSION_ISOLATION_MODE="shared_browser_node",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


class FakePage:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.on = unittest.mock.Mock()
        self.set_default_timeout = unittest.mock.Mock()
        self.goto = AsyncMock(side_effect=self._goto)
        self.title = AsyncMock(return_value="Fixture Page")
        self.add_init_script = AsyncMock()
        self._closed = False

    async def _goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        self.url = url

    def is_closed(self) -> bool:
        return self._closed


class FakePersistentContext:
    def __init__(self, pages: list[FakePage] | None = None) -> None:
        self.pages = pages if pages is not None else []
        self.on = unittest.mock.Mock()
        self.close = AsyncMock()
        self.tracing = unittest.mock.Mock()
        self.tracing.start = AsyncMock()
        self.tracing.stop = AsyncMock()
        self._new_page = FakePage()
        self.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})

    async def new_page(self) -> FakePage:
        self.pages.append(self._new_page)
        return self._new_page


class FakeCdpBrowser:
    def __init__(self) -> None:
        self.close = AsyncMock()


def _handle(name: str = "owner-default", *, already_open=False, seeded=False, was_empty=True):
    return PersistentProfileHandle(
        name=name,
        cdp_endpoint=f"ws://browser-node:9225/cdp/{name}/devtools/browser/abc",
        already_open=already_open,
        seeded=seeded,
        was_empty=was_empty,
    )


class _Base(unittest.IsolatedAsyncioTestCase):
    settings_overrides: dict = {}

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = BrowserManager(_settings(self.root, **self.settings_overrides))
        self.manager.audit.append = AsyncMock()
        self.manager._persist_session = AsyncMock()
        self.manager._settle = AsyncMock()
        self.manager._maybe_provision_session_tunnel = AsyncMock()
        self.manager.session_store.upsert = AsyncMock()
        self.contexts: list[FakePersistentContext] = []
        self.browsers: list[FakeCdpBrowser] = []
        self.handle_kwargs: dict = {}
        self.context_pages: list[FakePage] | None = None
        self.manager.persistent_profiles.open = AsyncMock(side_effect=self._fake_open)
        self.manager.persistent_profiles.close = AsyncMock(return_value=True)
        self.manager.runtime.attach_persistent_context = AsyncMock(side_effect=self._fake_attach)

    async def asyncTearDown(self) -> None:
        for session in list(self.manager.sessions.values()):
            if session.auto_persist_task is not None:
                session.auto_persist_task.cancel()
        self.tmp.cleanup()

    async def _fake_open(self, name, *, owner=None, context_kwargs=None, storage_state=None):
        await asyncio.sleep(0.01)  # a real launch awaits; lets concurrent Opens interleave
        return _handle(name, **self.handle_kwargs)

    async def _fake_attach(self, handle):
        context = FakePersistentContext(list(self.context_pages) if self.context_pages is not None else [])
        browser = FakeCdpBrowser()
        self.contexts.append(context)
        self.browsers.append(browser)
        return PersistentProfileAttachment(browser=browser, context=context, handle=handle)

    def _write_profile(self, name: str, *, owner: str | None = None) -> None:
        profile_dir = self.root / "auth" / "profiles" / name
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "state.json").write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        if owner is not None:
            (profile_dir / "profile.json").write_text(json.dumps({"owner": owner}), encoding="utf-8")

    def _fresh_context_browser(self):
        page = FakePage()

        class _Ctx:
            def __init__(self) -> None:
                self.pages = [page]
                self.on = unittest.mock.Mock()
                self.close = AsyncMock()
                self.new_page = AsyncMock(return_value=page)

        ctx = _Ctx()

        class _Browser:
            def __init__(self) -> None:
                self.new_context = AsyncMock(return_value=ctx)

        browser = _Browser()
        self.manager._acquire_session_browser = AsyncMock(return_value=(browser, None))
        return browser, ctx


class PersistentProfileSessionCreateTests(_Base):
    async def test_no_named_profile_uses_the_auto_persist_default(self) -> None:
        result = await self.manager.create_session(name="fixture")

        self.manager.persistent_profiles.open.assert_awaited_once()
        self.assertEqual(self.manager.persistent_profiles.open.await_args.args[0], "owner-default")
        self.assertIsNone(self.manager.persistent_profiles.open.await_args.kwargs["owner"])
        session = self.manager.sessions[result["id"]]
        self.assertEqual(session.persistent_profile_name, "owner-default")
        self.assertIs(session.context, self.contexts[0])
        self.assertFalse(session.remembered_login_loaded)

    async def test_named_auth_profile_is_used_and_locale_pinned(self) -> None:
        self.handle_kwargs = {"was_empty": False}
        self._write_profile("nihad-google")

        result = await self.manager.create_session(name="fixture", auth_profile="nihad-google")

        call = self.manager.persistent_profiles.open.await_args
        self.assertEqual(call.args[0], "nihad-google")
        self.assertEqual(call.kwargs["context_kwargs"]["locale"], "ar-EG")
        self.assertEqual(call.kwargs["context_kwargs"]["timezone_id"], "Africa/Cairo")
        self.assertNotIn("extra_http_headers", call.kwargs["context_kwargs"])
        self.assertEqual(call.kwargs["storage_state"], {"cookies": [], "origins": []})
        self.assertTrue(self.manager.sessions[result["id"]].remembered_login_loaded)

    async def test_owner_of_a_named_profile_is_sent_to_browser_node(self) -> None:
        self._write_profile("alice-login", owner="alice")
        token = set_current_operator("alice", source="token")
        try:
            await self.manager.create_session(name="fixture", auth_profile="alice-login")
        finally:
            reset_current_operator(token)
        self.assertEqual(self.manager.persistent_profiles.open.await_args.kwargs["owner"], "alice")

    async def test_seeded_fresh_profile_is_reported_as_remembered_login(self) -> None:
        self.handle_kwargs = {"seeded": True, "was_empty": True}
        result = await self.manager.create_session(name="fixture")
        self.assertTrue(self.manager.sessions[result["id"]].remembered_login_loaded)

    async def test_existing_tab_is_adopted_instead_of_opening_another(self) -> None:
        existing_tab = FakePage(url="https://example.com/already-open")
        self.context_pages = [existing_tab]
        result = await self.manager.create_session(name="fixture")
        self.assertIs(self.manager.sessions[result["id"]].page, existing_tab)

    async def test_profile_without_tabs_gets_a_new_page_and_tracing(self) -> None:
        self.manager.settings.enable_tracing = True
        self.context_pages = []
        result = await self.manager.create_session(name="fixture")
        session = self.manager.sessions[result["id"]]
        self.assertIs(session.page, self.contexts[0]._new_page)
        self.contexts[0].tracing.start.assert_awaited_once()

    async def test_stealth_injects_nothing_into_a_persistent_profile(self) -> None:
        """navigator.webdriver is kept false by launch switches in browser-node;
        a JS override would itself be a detectable own property."""
        self.manager.settings.stealth_enabled = True
        await self.manager.create_session(name="fixture")
        self.contexts[0]._new_page.add_init_script.assert_not_awaited()


class DeniedRememberedLoginTests(_Base):
    """Finding 2: a denied owner-default must never open the on-disk profile."""

    async def test_denied_owner_default_opens_a_plain_fresh_context(self) -> None:
        self._write_profile("owner-default", owner="alice")
        browser, ctx = self._fresh_context_browser()
        token = set_current_operator("mallory", source="token")
        try:
            result = await self.manager.create_session(name="fixture")
        finally:
            reset_current_operator(token)

        self.manager.persistent_profiles.open.assert_not_awaited()
        browser.new_context.assert_awaited_once()
        self.assertNotIn("storage_state", browser.new_context.await_args.kwargs)
        session = self.manager.sessions[result["id"]]
        self.assertIsNone(session.persistent_profile_name)
        self.assertFalse(session.remembered_login_loaded)

    async def test_unverified_caller_cannot_open_an_owned_default_profile(self) -> None:
        self._write_profile("owner-default", owner="alice")
        self._fresh_context_browser()
        await self.manager.create_session(name="fixture")  # no operator at all
        self.manager.persistent_profiles.open.assert_not_awaited()

    async def test_auto_persist_disabled_means_no_remembered_profile(self) -> None:
        self.manager.settings.auto_persist_login_enabled = False
        browser, _ctx = self._fresh_context_browser()
        await self.manager.create_session(name="fixture")
        self.manager.persistent_profiles.open.assert_not_awaited()
        browser.new_context.assert_awaited_once()

    async def test_denied_named_profile_still_raises(self) -> None:
        self._write_profile("alice-login", owner="alice")
        token = set_current_operator("mallory", source="token")
        try:
            with self.assertRaises(PermissionError):
                await self.manager.create_session(name="fixture", auth_profile="alice-login")
        finally:
            reset_current_operator(token)
        self.manager.persistent_profiles.open.assert_not_awaited()


class ForkAndExplicitStateTests(_Base):
    """Finding 6: fork()/storage_state_path use the shared browser-node browser."""

    async def test_explicit_storage_state_path_uses_a_fresh_context_on_the_shared_browser(self) -> None:
        state_path = self.root / "auth" / "forked-state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        browser, _ctx = self._fresh_context_browser()

        result = await self.manager.create_session(name="fork-fixture", storage_state_path=str(state_path))

        self.manager.persistent_profiles.open.assert_not_awaited()
        browser.new_context.assert_awaited_once()
        self.assertIn("storage_state", browser.new_context.await_args.kwargs)
        self.assertIsNone(self.manager.sessions[result["id"]].persistent_profile_name)


class ExclusiveLeaseTests(_Base):
    """Finding 4: one live session per profile; close releases exactly once."""

    async def test_second_open_of_the_same_profile_returns_the_live_session(self) -> None:
        first = await self.manager.create_session(name="one")
        second = await self.manager.create_session(name="two")

        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["reused_existing_session"])
        self.manager.persistent_profiles.open.assert_awaited_once()
        self.assertEqual(len(self.manager.sessions), 1)

    async def test_concurrent_opens_of_the_same_profile_launch_once(self) -> None:
        results = await asyncio.gather(
            self.manager.create_session(name="a"),
            self.manager.create_session(name="b"),
            self.manager.create_session(name="c"),
        )
        self.assertEqual(len({r["id"] for r in results}), 1)
        self.manager.persistent_profiles.open.assert_awaited_once()
        self.manager.runtime.attach_persistent_context.assert_awaited_once()

    async def test_close_disconnects_and_releases_exactly_once(self) -> None:
        result = await self.manager.create_session(name="fixture")
        session = self.manager.sessions[result["id"]]
        await self.manager.close_session(result["id"])

        self.manager.persistent_profiles.close.assert_awaited_once_with("owner-default")
        self.browsers[0].close.assert_awaited_once()  # CDP disconnect
        self.contexts[0].close.assert_not_awaited()  # never close() the profile's own context
        # A late retirement of the same session must not release it again.
        async with session.lock:
            await self.manager.session_lifecycle._retire_dead_session(session, reason="test")
        await self.manager.session_lifecycle.release_persistent_profile(session)
        self.manager.persistent_profiles.close.assert_awaited_once()

    async def test_after_close_a_new_open_launches_again(self) -> None:
        first = await self.manager.create_session(name="one")
        await self.manager.close_session(first["id"])
        second = await self.manager.create_session(name="two")
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(self.manager.persistent_profiles.open.await_count, 2)

    async def test_attach_failure_releases_the_profile_once(self) -> None:
        self.manager.runtime.attach_persistent_context = AsyncMock(side_effect=RuntimeError("relay down"))
        with self.assertRaises(RuntimeError):
            await self.manager.create_session(name="fixture")
        self.manager.persistent_profiles.close.assert_awaited_once_with("owner-default")
        self.assertEqual(self.manager.sessions, {})
        self.assertEqual(self.manager._session_reservations, set())

    async def test_open_failure_does_not_release_what_was_never_opened(self) -> None:
        self.manager.persistent_profiles.open = AsyncMock(
            side_effect=PersistentProfileError("refused", status_code=409)
        )
        with self.assertRaises(PersistentProfileError):
            await self.manager.create_session(name="fixture")
        self.manager.persistent_profiles.close.assert_not_awaited()
        self.assertEqual(self.manager._session_reservations, set())

    async def test_failure_after_attach_disconnects_and_releases_once(self) -> None:
        self.manager._maybe_provision_session_tunnel = AsyncMock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            await self.manager.create_session(name="fixture")
        self.manager.persistent_profiles.close.assert_awaited_once_with("owner-default")
        self.browsers[0].close.assert_awaited_once()
        self.contexts[0].close.assert_not_awaited()


class SessionLimitTests(_Base):
    async def test_persistent_mode_allows_one_visible_browser_even_with_higher_max_sessions(self) -> None:
        self._write_profile("nihad-google")
        await self.manager.create_session(name="one")
        with self.assertRaisesRegex(RuntimeError, "Session limit reached: max_sessions=1"):
            await self.manager.create_session(name="two", auth_profile="nihad-google")
        self.manager.persistent_profiles.open.assert_awaited_once()

    async def test_concurrent_opens_of_different_profiles_cannot_both_pass_the_limit(self) -> None:
        self._write_profile("nihad-google")
        results = await asyncio.gather(
            self.manager.create_session(name="one"),
            self.manager.create_session(name="two", auth_profile="nihad-google"),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(errors), 1, results)
        self.assertIn("Session limit reached", str(errors[0]))
        self.assertEqual(len(self.manager.sessions), 1)


class LegacySessionLimitTests(_Base):
    settings_overrides = {"PERSISTENT_PROFILES_ENABLED": False, "MAX_SESSIONS": 1}

    async def test_concurrent_opens_are_limited_atomically(self) -> None:
        async def slow_acquire(_session_id):
            await asyncio.sleep(0.02)
            page = FakePage()
            ctx = unittest.mock.Mock()
            ctx.pages = [page]
            ctx.new_page = AsyncMock(return_value=page)
            browser = unittest.mock.Mock()
            browser.new_context = AsyncMock(return_value=ctx)
            return browser, None

        self.manager._acquire_session_browser = AsyncMock(side_effect=slow_acquire)
        results = await asyncio.gather(
            self.manager.create_session(name="one"),
            self.manager.create_session(name="two"),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(errors), 1, results)
        self.assertEqual(len(self.manager.sessions), 1)
        self.manager.persistent_profiles.open.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
