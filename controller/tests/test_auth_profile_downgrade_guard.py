"""Regression tests for the auto-persist downgrade guard.

The incident: a session that never actually held the owner's Facebook login
(auto-load failed, a fresh context, a site that kicked the session) still ran
the periodic auto-persist writer, which silently overwrote the one saved copy
of that login with the session's logged-out state. There was no history to
recover from either.

These tests pin two independent behaviours:

- `save_auto_persist` (the background writer's only entry point) refuses to
  overwrite a saved profile when doing so would lose a site it is currently
  signed into, and says so at WARNING.
- `save_for_session` (what an explicit, operator-chosen save goes through)
  keeps overwriting regardless -- the owner asked for it -- but every write
  still rotates the previous file into history first.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.auth_state import AuthStateManager
from app.browser.services.auth_profiles import BrowserAuthProfileService


def _facebook_cookies() -> list[dict]:
    return [
        {"name": "c_user", "value": "1000123", "domain": ".facebook.com", "expires": -1},
        {"name": "xs", "value": "abc123", "domain": ".facebook.com", "expires": -1},
    ]


class _StubPage:
    url = "https://facebook.com/"

    async def title(self) -> str:
        return "Facebook"


class _FakeContext:
    """Mimics Playwright's `context.storage_state()`, with and without `path`."""

    def __init__(self, cookies: list[dict]) -> None:
        self._cookies = cookies

    async def storage_state(self, path: str | None = None):
        body = {"cookies": self._cookies, "origins": []}
        if path:
            Path(path).write_text(json.dumps(body), encoding="utf-8")
            return None
        return body


class _StubSession:
    def __init__(self, context: _FakeContext) -> None:
        self.id = "session-1"
        self.context = context
        self.page = _StubPage()
        self.last_auth_state_path = None
        self.auth_profile_name = None


class _StubAudit:
    async def append(self, **_kwargs) -> None:
        return None


class AutoPersistDowngradeGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.auth_root = Path(self._tmp.name)
        self.auth_state = AuthStateManager(encryption_key=None, require_encryption=False, max_age_hours=72)
        manager = SimpleNamespace(
            settings=SimpleNamespace(auth_root=str(self.auth_root), auth_state_encryption_key=None),
            auth_state=self.auth_state,
            audit=_StubAudit(),
            witness=None,
        )
        self.service = BrowserAuthProfileService(manager)
        self.profile_dir = self.auth_root / "profiles" / "owner-default"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_existing_profile(self, cookies: list[dict]) -> Path:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        state_path = self.profile_dir / "state.json"
        state_path.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
        return state_path

    async def test_first_save_has_nothing_to_compare_against_and_proceeds(self) -> None:
        session = _StubSession(_FakeContext([]))

        result = await self.service.save_auto_persist(session, "owner-default")

        self.assertNotIn("skipped", result)
        self.assertTrue((self.profile_dir / "state.json").exists())

    async def test_skips_and_warns_when_facebook_login_would_be_lost(self) -> None:
        state_path = self._seed_existing_profile(_facebook_cookies())
        # The live session never actually holds the Facebook cookies -- this is
        # the exact shape of the incident (auto-load failure, or a session that
        # just never signed in).
        session = _StubSession(_FakeContext([]))

        with self.assertLogs("app.browser.services.auth_profiles", level="WARNING") as captured:
            result = await self.service.save_auto_persist(session, "owner-default")

        self.assertTrue(result.get("skipped"))
        self.assertEqual(result["lost_sites"], ["facebook.com"])
        self.assertTrue(any("facebook.com" in message for message in captured.output))
        # The old, good copy must be untouched.
        stored = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["cookies"], _facebook_cookies())

    async def test_proceeds_when_the_signed_in_site_is_still_present(self) -> None:
        self._seed_existing_profile(_facebook_cookies())
        session = _StubSession(_FakeContext(_facebook_cookies()))

        result = await self.service.save_auto_persist(session, "owner-default")

        self.assertNotIn("skipped", result)

    async def test_proceeds_when_the_saved_profile_was_never_signed_into_anything(self) -> None:
        # No recognised sign-in cookie in the saved profile -- nothing to lose,
        # so the guard has nothing to say either way.
        self._seed_existing_profile([{"name": "unrelated", "value": "x", "domain": "example.com"}])
        session = _StubSession(_FakeContext([]))

        result = await self.service.save_auto_persist(session, "owner-default")

        self.assertNotIn("skipped", result)

    async def test_explicit_save_still_overwrites_a_would_be_lost_login_but_rotates_history(self) -> None:
        state_path = self._seed_existing_profile(_facebook_cookies())
        session = _StubSession(_FakeContext([]))  # the owner chose to save this logged-out state

        await self.service.save_for_session(session, "owner-default")

        stored = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["cookies"], [])
        history_files = list(self.profile_dir.glob("state.json.*"))
        self.assertEqual(len(history_files), 1)
        rotated = json.loads(history_files[0].read_text(encoding="utf-8"))
        self.assertEqual(rotated["cookies"], _facebook_cookies())


if __name__ == "__main__":
    unittest.main()
