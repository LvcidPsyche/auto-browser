"""A session's saved auth state belongs to the operator who owns that session.

Auth profiles got an owner in 1.8.1 and sessions in 1.9.0, but the state a
session writes into its own auth directory (AUTH_ROOT/<session_id>/) was left
out. `storage_state_path` is relative to AUTH_ROOT, so under named credentials
(API_BEARER_TOKENS) operator Bob could ask for `<alice's session id>/state.json`
and get a browser logged in as Alice. The owner is recorded in a marker next to
the state, so the answer survives the session closing and a controller restart.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager
from app.models import OperatorIdentity
from app.session_ownership import acting_as
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage, _settings

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")


class StateContext(FakeContext):
    """A context whose storage_state() carries a recognisable cookie."""

    def __init__(self, page: FakePage, marker: str = "alice-cookie") -> None:
        super().__init__(page)
        self.marker = marker

    async def storage_state(self, path: str | None = None) -> dict:
        state = {
            "cookies": [{"name": "sid", "value": self.marker, "domain": "bank.example", "path": "/"}],
            "origins": [],
        }
        if path:
            Path(path).write_text(json.dumps(state), encoding="utf-8")
        return state


def build_manager(root: Path) -> tuple[BrowserManager, AsyncMock]:
    manager = BrowserManager(_settings(root))
    manager._settle = AsyncMock()  # type: ignore[method-assign]
    manager._maybe_provision_session_tunnel = AsyncMock()  # type: ignore[method-assign]
    browser = FakeBrowser(StateContext(FakePage()))
    browser.new_context = AsyncMock(side_effect=lambda **_kwargs: StateContext(FakePage(), marker="own"))
    manager.browser = browser  # type: ignore[assignment]
    manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]
    return manager, browser.new_context


class SessionAuthStateOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager, self.new_context = build_manager(self.root)
        self.manager.settings.max_sessions = 5

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _alice_saves_state(self, manager: BrowserManager | None = None) -> str:
        manager = manager or self.manager
        with acting_as(ALICE):
            session_id = (await manager.create_session(name="alice"))["id"]
            session = manager.sessions[session_id]
            session.context = StateContext(session.page)
            session.page.url = "https://example.com/"
            await manager.auth_profiles.save_storage_state(session_id, "state.json")
        return session_id

    async def _create_from(self, operator: OperatorIdentity | None, path: str, manager: BrowserManager | None = None):
        with acting_as(operator):
            return await (manager or self.manager).create_session(name="from-path", storage_state_path=path)

    async def test_another_operator_cannot_open_a_session_from_a_live_sessions_state(self) -> None:
        session_id = await self._alice_saves_state()

        with self.assertRaises(PermissionError):
            await self._create_from(BOB, f"{session_id}/state.json")
        self.assertEqual(len(self.manager.sessions), 1, "the refused request must not leave a session behind")

    async def test_the_owner_can_still_open_a_session_from_her_own_state(self) -> None:
        session_id = await self._alice_saves_state()

        created = await self._create_from(ALICE, f"{session_id}/state.json")

        self.assertEqual(created["owner"], "alice")
        self.assertIn("storage_state", self.new_context.await_args.kwargs)

    async def test_the_ban_outlives_the_session_and_a_controller_restart(self) -> None:
        session_id = await self._alice_saves_state()
        with acting_as(ALICE):
            await self.manager.close_session(session_id)
        # A new manager over the same roots has no live sessions and no memory.
        restarted, _ = build_manager(self.root)
        restarted.settings.max_sessions = 5
        # Even with the session record gone, only the marker is left to say who owns it.
        for record in (self.root / "sessions").glob("*.json"):
            record.unlink()

        with self.assertRaises(PermissionError):
            await self._create_from(BOB, f"{session_id}/state.json", restarted)
        created = await self._create_from(ALICE, f"{session_id}/state.json", restarted)
        self.assertEqual(created["owner"], "alice")

    async def test_a_forked_session_state_is_owned_too(self) -> None:
        # The fork's state file exists only while the new session loads it (NAV-02),
        # so Bob tries it in exactly that window.
        session_id = await self._alice_saves_state()
        create = self.manager.create_session
        refused: list[str] = []

        async def bob_tries_first(**kwargs):
            relative = Path(kwargs["storage_state_path"]).relative_to(self.root / "auth").as_posix()
            try:
                with acting_as(BOB):
                    await create(name="from-path", storage_state_path=relative)
            except PermissionError:
                refused.append(relative)
            return await create(**kwargs)

        self.manager.create_session = AsyncMock(side_effect=bob_tries_first)  # type: ignore[method-assign]
        with acting_as(ALICE):
            forked = await self.manager.fork_session(session_id)

        self.assertEqual(forked["owner"], "alice")
        self.assertEqual(len(refused), 1)
        self.assertRegex(refused[0], rf"^{session_id}/fork_")

    async def test_a_damaged_marker_does_not_read_as_unowned(self) -> None:
        session_id = await self._alice_saves_state()
        marker = self.root / "auth" / session_id / ".owner.json"
        self.assertTrue(marker.exists(), "saving state must leave an owner marker beside it")
        marker.write_text('{"owner": "al', encoding="utf-8")

        with self.assertRaises(PermissionError):
            await self._create_from(BOB, f"{session_id}/state.json")

    async def test_a_directory_without_a_marker_falls_back_to_the_session_record(self) -> None:
        """State written by a release that left no marker: the record still names the owner."""
        session_id = await self._alice_saves_state()
        for marker in (self.root / "auth" / session_id).glob(".*.json"):
            marker.unlink()

        with self.assertRaises(PermissionError):
            await self._create_from(BOB, f"{session_id}/state.json")
        created = await self._create_from(ALICE, f"{session_id}/state.json")
        self.assertEqual(created["owner"], "alice")

    async def test_without_a_marker_or_a_record_the_state_stays_open(self) -> None:
        """What happens today to state nobody can attribute: no owner to enforce."""
        session_id = await self._alice_saves_state()
        for marker in (self.root / "auth" / session_id).glob(".*.json"):
            marker.unlink()
        for record in (self.root / "sessions").glob("*.json"):
            record.unlink()
        self.manager.sessions.pop(session_id)

        created = await self._create_from(BOB, f"{session_id}/state.json")
        self.assertEqual(created["owner"], "bob")

    async def test_unowned_sessions_write_no_marker_and_stay_open(self) -> None:
        """Single-operator and shared-token deployments are unchanged."""
        with acting_as(None):
            session_id = (await self.manager.create_session(name="shared"))["id"]
            session = self.manager.sessions[session_id]
            session.context = StateContext(session.page)
            await self.manager.auth_profiles.save_storage_state(session_id, "state.json")
        self.assertEqual(list((self.root / "auth" / session_id).glob(".*.json")), [])

        created = await self._create_from(BOB, f"{session_id}/state.json")
        self.assertEqual(created["owner"], "bob")


if __name__ == "__main__":
    unittest.main()
