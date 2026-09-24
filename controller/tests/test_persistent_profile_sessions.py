from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser.services.runtime import PersistentProfileAttachment
from app.browser_manager import BrowserManager
from app.config import Settings
from app.persistent_profiles import PersistentProfileHandle


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

    async def _goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        self.url = url


class FakePersistentContext:
    def __init__(self, pages: list[FakePage] | None = None) -> None:
        self.pages = pages or []
        self.on = unittest.mock.Mock()
        self.close = AsyncMock()
        self.tracing = unittest.mock.Mock()
        self.tracing.start = AsyncMock()
        self._new_page = FakePage()

    async def new_page(self) -> FakePage:
        self.pages.append(self._new_page)
        return self._new_page


class FakeCdpBrowser:
    def __init__(self) -> None:
        self.close = AsyncMock()


def _attachment(
    *, already_open: bool, seeded: bool, was_empty: bool, pages: list[FakePage] | None = None
) -> tuple[PersistentProfileAttachment, FakePersistentContext]:
    context = FakePersistentContext(pages)
    browser = FakeCdpBrowser()
    handle = PersistentProfileHandle(
        name="owner-default",
        cdp_endpoint="ws://browser-node:1234/devtools/browser/abc",
        already_open=already_open,
        seeded=seeded,
        was_empty=was_empty,
    )
    return PersistentProfileAttachment(browser=browser, context=context, handle=handle), context


class PersistentProfileSessionCreateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = BrowserManager(_settings(self.root))
        self.manager.audit.append = AsyncMock()
        self.manager._persist_session = AsyncMock()
        self.manager._settle = AsyncMock()
        self.manager._maybe_provision_session_tunnel = AsyncMock()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_no_named_profile_uses_the_auto_persist_default(self) -> None:
        attachment, context = _attachment(already_open=False, seeded=False, was_empty=True)
        self.manager.runtime.acquire_persistent_context = AsyncMock(return_value=attachment)

        result = await self.manager.create_session(name="fixture")

        self.manager.runtime.acquire_persistent_context.assert_awaited_once()
        call_kwargs = self.manager.runtime.acquire_persistent_context.await_args.kwargs
        self.assertEqual(call_kwargs["profile_name"], "owner-default")
        session = self.manager.sessions[result["id"]]
        self.assertEqual(session.persistent_profile_name, "owner-default")
        self.assertIs(session.context, context)
        # Fresh (never-seeded) profile: nothing to report as "remembered".
        self.assertFalse(session.remembered_login_loaded)

    async def test_named_auth_profile_is_used_and_locale_pinned(self) -> None:
        attachment, _context = _attachment(already_open=False, seeded=False, was_empty=False)
        self.manager.runtime.acquire_persistent_context = AsyncMock(return_value=attachment)
        profile_dir = self.root / "auth" / "profiles" / "nihad-google"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "state.json").write_text('{"cookies": [], "origins": []}', encoding="utf-8")

        result = await self.manager.create_session(name="fixture", auth_profile="nihad-google")

        call_kwargs = self.manager.runtime.acquire_persistent_context.await_args.kwargs
        self.assertEqual(call_kwargs["profile_name"], "nihad-google")
        self.assertEqual(call_kwargs["context_kwargs"]["locale"], "ar-EG")
        self.assertEqual(call_kwargs["context_kwargs"]["timezone_id"], "Africa/Cairo")
        self.assertNotIn("extra_http_headers", call_kwargs["context_kwargs"])
        session = self.manager.sessions[result["id"]]
        # was_empty=False (existing on-disk profile): treated as already logged in.
        self.assertTrue(session.remembered_login_loaded)

    async def test_seeded_fresh_profile_is_reported_as_remembered_login(self) -> None:
        attachment, _context = _attachment(already_open=False, seeded=True, was_empty=True)
        self.manager.runtime.acquire_persistent_context = AsyncMock(return_value=attachment)

        result = await self.manager.create_session(name="fixture")
        session = self.manager.sessions[result["id"]]
        self.assertTrue(session.remembered_login_loaded)

    async def test_already_open_profile_adopts_the_latest_tab_instead_of_a_new_one(self) -> None:
        existing_tab = FakePage(url="https://example.com/already-open")
        attachment, context = _attachment(
            already_open=True, seeded=False, was_empty=False, pages=[existing_tab]
        )
        self.manager.runtime.acquire_persistent_context = AsyncMock(return_value=attachment)

        result = await self.manager.create_session(name="fixture")

        session = self.manager.sessions[result["id"]]
        self.assertIs(session.page, existing_tab)
        context.tracing.start.assert_not_awaited()

    async def test_fresh_profile_starts_a_new_page_and_tracing_when_enabled(self) -> None:
        self.manager.settings.enable_tracing = True
        attachment, context = _attachment(already_open=False, seeded=False, was_empty=True)
        self.manager.runtime.acquire_persistent_context = AsyncMock(return_value=attachment)

        result = await self.manager.create_session(name="fixture")

        session = self.manager.sessions[result["id"]]
        self.assertIs(session.page, context._new_page)
        context.tracing.start.assert_awaited_once()

    async def test_explicit_storage_state_path_bypasses_persistent_profiles(self) -> None:
        """fork()'s clone-this-session path must get its own fresh context, not
        collapse onto the same running persistent profile."""
        state_path = self.root / "auth" / "forked-state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        self.manager.runtime.acquire_persistent_context = AsyncMock()

        page = FakePage()

        class _FakeEphemeralContext:
            def __init__(self) -> None:
                self.pages = [page]
                self.on = unittest.mock.Mock()
                self.close = AsyncMock()
                self.new_page = AsyncMock(return_value=page)

        ephemeral_context = _FakeEphemeralContext()

        class _FakeEphemeralBrowser:
            def __init__(self) -> None:
                self.new_context = AsyncMock(return_value=ephemeral_context)

        ephemeral_browser = _FakeEphemeralBrowser()
        self.manager._acquire_session_browser = AsyncMock(return_value=(ephemeral_browser, None))

        result = await self.manager.create_session(name="fork-fixture", storage_state_path=str(state_path))

        self.manager.runtime.acquire_persistent_context.assert_not_awaited()
        ephemeral_browser.new_context.assert_awaited_once()
        session = self.manager.sessions[result["id"]]
        self.assertIsNone(session.persistent_profile_name)

    async def test_stealth_uses_the_safe_persistent_script(self) -> None:
        self.manager.settings.stealth_enabled = True
        attachment, context = _attachment(already_open=False, seeded=False, was_empty=True)
        self.manager.runtime.acquire_persistent_context = AsyncMock(return_value=attachment)

        await self.manager.create_session(name="fixture")

        context._new_page.add_init_script.assert_awaited_once()
        script = context._new_page.add_init_script.await_args.args[0]
        # The safe subset only removes navigator.webdriver -- it must not
        # touch navigator.languages (that would fight the pinned ar-EG locale)
        # or add canvas/webgl noise on a real, headed profile.
        self.assertIn("webdriver", script)
        self.assertNotIn("navigator.languages", script)
        self.assertNotIn("toDataURL", script)


class PersistentProfileSessionCloseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = BrowserManager(_settings(self.root))
        self.manager.audit.append = AsyncMock()
        self.manager._persist_session = AsyncMock()
        self.manager._settle = AsyncMock()
        self.manager._maybe_provision_session_tunnel = AsyncMock()
        self.manager.session_store.upsert = AsyncMock()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_close_releases_the_persistent_profile(self) -> None:
        attachment, _context = _attachment(already_open=False, seeded=False, was_empty=True)
        self.manager.runtime.acquire_persistent_context = AsyncMock(return_value=attachment)
        self.manager.persistent_profiles.close = AsyncMock()

        result = await self.manager.create_session(name="fixture")
        await self.manager.close_session(result["id"])

        self.manager.persistent_profiles.close.assert_awaited_once_with("owner-default")


if __name__ == "__main__":
    unittest.main()
