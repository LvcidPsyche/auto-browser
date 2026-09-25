"""Regression tests for the "remembered login" incident.

The owner's remembered browser login (the default "remember me" auth
profile) was silently erased: a session that failed to load it went on to
auto-persist its own logged-out state over the good copy, with no history to
recover from and no signal that anything had gone wrong. These tests pin the
three independent fixes:

- an auto-load of an old (but not truly ancient) remembered login still
  succeeds -- the 72h staleness check does not apply to the unattended
  "remember me" path;
- a session whose auto-load genuinely failed (profile too old even for the
  relaxed limit, or otherwise unreadable) never auto-persists over the saved
  profile;
- the session summary tells the caller whether the remembered login loaded.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager
from app.config import Settings
from app.utils import UTC


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
        # Large enough that the periodic loop (exercised by its own tests)
        # never actually fires during these tests; kept > 0 so we can assert
        # on whether create_session decided to start it at all.
        AUTO_PERSIST_INTERVAL_SECONDS=9999,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.on = unittest.mock.Mock()
        self.set_default_timeout = unittest.mock.Mock()
        self.goto = AsyncMock(side_effect=self._goto)
        self.title = AsyncMock(return_value="Fixture Page")
        self.is_closed = lambda: False

    async def _goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        self.url = url


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.pages = [page]
        self.on = unittest.mock.Mock()
        self.close = AsyncMock()
        self.new_page = AsyncMock(return_value=page)

    async def storage_state(self, path: str | None = None) -> dict:
        body = {"cookies": [], "origins": []}
        if path:
            Path(path).write_text(json.dumps(body), encoding="utf-8")
            return None
        return body


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.new_context = AsyncMock(return_value=context)


def _write_profile_state(auth_root: Path, profile_name: str, *, age_hours: float) -> Path:
    profile_dir = auth_root / "profiles" / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    state_path = profile_dir / "state.json"
    state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    old_timestamp = datetime.now(UTC).timestamp() - age_hours * 3600
    os.utime(state_path, (old_timestamp, old_timestamp))
    return state_path


class RememberedLoginCreateSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = _settings(self.root)
        self.manager = BrowserManager(self.settings)
        self.manager.audit.append = AsyncMock()
        self.manager._persist_session = AsyncMock()
        self.manager._settle = AsyncMock()
        self.manager._maybe_provision_session_tunnel = AsyncMock()

    async def asyncTearDown(self) -> None:
        for session in self.manager.sessions.values():
            if session.auto_persist_task is not None:
                session.auto_persist_task.cancel()
        self.tmp.cleanup()

    def _patch_browser(self) -> FakePage:
        page = FakePage()
        context = FakeContext(page)
        browser = FakeBrowser(context)
        self.manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]
        return page

    async def test_old_but_not_ancient_remembered_login_still_loads(self) -> None:
        """72h between browser opens must not be what silently erases a login."""
        self._patch_browser()
        _write_profile_state(Path(self.settings.auth_root), "owner-default", age_hours=100)

        result = await self.manager.create_session(name="fixture")

        session = self.manager.sessions[result["id"]]
        self.assertTrue(session.remembered_login_loaded)
        self.assertIsNone(session.remembered_login_error)
        self.assertEqual(session.auto_persist_profile_name, "owner-default")

    async def test_ancient_remembered_login_fails_to_load_and_blocks_auto_persist(self) -> None:
        """A profile stale even under the relaxed unattended limit must not load --
        and, crucially, this session must never be allowed to auto-persist over it
        (belt and braces with the downgrade guard)."""
        self._patch_browser()
        # AUTH_STATE_UNATTENDED_MAX_AGE_HOURS defaults to 2160h (90 days).
        _write_profile_state(Path(self.settings.auth_root), "owner-default", age_hours=3000)

        result = await self.manager.create_session(name="fixture")

        session = self.manager.sessions[result["id"]]
        self.assertFalse(session.remembered_login_loaded)
        self.assertIsNotNone(session.remembered_login_error)
        self.assertIsNone(session.auto_persist_profile_name)

        # Belt and braces: even if something tried to auto-persist for this
        # session, close() must not, since auto_persist_profile_name is unset.
        self.manager.auth_profiles.save_auto_persist = AsyncMock()
        await self.manager.close_session(session.id)
        self.manager.auth_profiles.save_auto_persist.assert_not_awaited()

    async def test_no_remembered_profile_yet_is_not_an_error(self) -> None:
        self._patch_browser()

        result = await self.manager.create_session(name="fixture")

        session = self.manager.sessions[result["id"]]
        self.assertFalse(session.remembered_login_loaded)
        self.assertIsNone(session.remembered_login_error)

    async def test_session_summary_exposes_remembered_login_loaded(self) -> None:
        self._patch_browser()
        _write_profile_state(Path(self.settings.auth_root), "owner-default", age_hours=1)

        result = await self.manager.create_session(name="fixture")
        session = self.manager.sessions[result["id"]]

        summary = await self.manager._session_summary(session)

        self.assertIn("remembered_login_loaded", summary)
        self.assertTrue(summary["remembered_login_loaded"])
        self.assertIn("remembered_login_error", summary)

    async def test_unattended_cron_session_loads_named_profile_past_the_interactive_limit(self) -> None:
        """A named profile (e.g. "nihad-google") opened by an unattended cron job has
        the same silent-failure risk as the default profile: nobody is there to notice
        a stale-refusal and re-authenticate, so it gets the same relaxed limit."""
        self._patch_browser()
        _write_profile_state(Path(self.settings.auth_root), "nihad-google", age_hours=100)

        result = await self.manager.create_session(
            name="cron-job", auth_profile="nihad-google", unattended=True
        )

        session = self.manager.sessions[result["id"]]
        self.assertEqual(session.auth_profile_name, "nihad-google")

    async def test_interactive_session_still_enforces_the_tight_staleness_limit(self) -> None:
        """A human explicitly opening a named profile keeps the tighter 72h check --
        they are there to notice the refusal and re-authenticate."""
        self._patch_browser()
        _write_profile_state(Path(self.settings.auth_root), "nihad-google", age_hours=100)

        with self.assertRaises(PermissionError):
            await self.manager.create_session(name="interactive", auth_profile="nihad-google")

    async def test_named_profile_session_auto_persists_into_that_profile_not_owner_default(self) -> None:
        """The proven incident: opening 'nihad-google' still only ever refreshed
        'owner-default', so a named profile that is loaded but never re-saved goes
        dead once the site rotates its session cookies. Auto-persist must target
        the profile the session was actually opened from."""
        self._patch_browser()
        _write_profile_state(Path(self.settings.auth_root), "nihad-google", age_hours=1)

        result = await self.manager.create_session(name="fixture", auth_profile="nihad-google")

        session = self.manager.sessions[result["id"]]
        self.assertEqual(session.auth_profile_name, "nihad-google")
        self.assertEqual(session.auto_persist_profile_name, "nihad-google")

    async def test_named_profile_session_close_saves_into_that_profile_not_owner_default(self) -> None:
        self._patch_browser()
        _write_profile_state(Path(self.settings.auth_root), "nihad-google", age_hours=1)

        result = await self.manager.create_session(name="fixture", auth_profile="nihad-google")
        session = self.manager.sessions[result["id"]]

        self.manager.auth_profiles.save_auto_persist = AsyncMock()
        await self.manager.close_session(session.id)

        self.manager.auth_profiles.save_auto_persist.assert_awaited_once_with(session, "nihad-google")

    async def test_no_profile_session_still_auto_persists_into_owner_default(self) -> None:
        """Sessions opened with no named profile keep today's behaviour."""
        self._patch_browser()

        result = await self.manager.create_session(name="fixture")

        session = self.manager.sessions[result["id"]]
        self.assertIsNone(session.auth_profile_name)
        self.assertEqual(session.auto_persist_profile_name, "owner-default")


if __name__ == "__main__":
    unittest.main()
