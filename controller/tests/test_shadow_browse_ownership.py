"""A shadow-browse session carries the owner of the session it was made from.

enable_shadow_browse copies a live session's cookies into a new headed browser.
The copy was created with no owner, so under named credentials (API_BEARER_TOKENS)
any other operator could list it, drive it and read the original owner's logged-in
pages. The copy is the same logins, so it belongs to the same operator.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.action_errors import SessionNotFoundError
from app.models import OperatorIdentity
from app.session_ownership import acting_as
from tests.test_auth_state_ownership import StateContext, build_manager
from tests.test_browser_manager_create_session import FakeBrowser, FakePage

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")


class ShadowBrowseOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager, _ = build_manager(Path(self.tmp.name))
        self.manager.settings.shadow_browse_enabled = True
        self.manager.settings.max_sessions = 5
        headed = FakeBrowser(StateContext(FakePage(), "shadow"))
        headed.close = AsyncMock()
        headed.new_context = AsyncMock(return_value=StateContext(FakePage(), "shadow"))
        self.manager.playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=headed)))  # type: ignore[assignment]

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _shadow_of(self, operator: OperatorIdentity | None) -> tuple[str, str]:
        with acting_as(operator):
            session_id = (await self.manager.create_session(name="source"))["id"]
            self.manager.sessions[session_id].context = StateContext(self.manager.sessions[session_id].page)
            shadow = await self.manager.enable_shadow_browse(session_id)
        return session_id, shadow["shadow_session_id"]

    async def test_the_shadow_session_belongs_to_the_source_owner(self) -> None:
        _, shadow_id = await self._shadow_of(ALICE)

        self.assertEqual(self.manager.sessions[shadow_id].owner, "alice")
        with acting_as(BOB), self.assertRaises(SessionNotFoundError):
            await self.manager.get_session(shadow_id)
        with acting_as(BOB):
            self.assertNotIn(shadow_id, [s["id"] for s in await self.manager.list_sessions()])
        with acting_as(ALICE):
            self.assertEqual((await self.manager.get_session(shadow_id)).id, shadow_id)

    async def test_the_shadow_of_an_unowned_session_stays_unowned(self) -> None:
        _, shadow_id = await self._shadow_of(None)

        self.assertIsNone(self.manager.sessions[shadow_id].owner)
        with acting_as(BOB):
            self.assertEqual((await self.manager.get_session(shadow_id)).id, shadow_id)

    async def test_shadow_browse_is_not_held_to_the_session_limit(self) -> None:
        """It replaces the session being debugged; a limit check would break MAX_SESSIONS=1."""
        self.manager.settings.max_sessions = 1

        _, shadow_id = await self._shadow_of(ALICE)

        self.assertIn(shadow_id, self.manager.sessions)


if __name__ == "__main__":
    unittest.main()
