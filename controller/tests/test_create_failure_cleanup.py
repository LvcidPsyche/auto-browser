"""A create that fails leaves no directories and no active record (NAV-35)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.test_auth_state_ownership import build_manager

SESSION_ID = "abcdef012345"


class CreateFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager, _ = build_manager(self.root)
        patcher = patch("app.browser.services.session_creation.new_session_id", return_value=SESSION_ID)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def leftovers(self) -> list[str]:
        return [
            str(path.relative_to(self.root))
            for name in ("artifacts", "auth", "uploads")
            if (path := self.root / name / SESSION_ID).exists()
        ]

    async def test_a_refused_request_creates_no_directories(self) -> None:
        # No PROXY_PERSONA_FILE: refused after the id is minted, before any browser opens.
        with self.assertRaises(RuntimeError):
            await self.manager.create_session(proxy_persona="nobody")

        self.assertEqual(self.leftovers(), [])

    async def test_a_missing_auth_profile_creates_no_directories(self) -> None:
        with self.assertRaises(Exception):
            await self.manager.create_session(auth_profile="no-such-profile")

        self.assertEqual(self.leftovers(), [])

    async def test_a_browser_that_will_not_start_removes_the_directories(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(side_effect=RuntimeError("no browser"))  # type: ignore[method-assign]

        with self.assertRaises(RuntimeError):
            await self.manager.create_session()

        self.assertEqual(self.leftovers(), [])

    async def test_a_failure_after_the_first_record_leaves_no_active_record(self) -> None:
        self.manager.witness_bridge.record_session_receipt = AsyncMock(side_effect=RuntimeError("witness down"))  # type: ignore[method-assign]

        with self.assertRaises(RuntimeError):
            await self.manager.create_session()

        self.assertEqual(self.leftovers(), [])
        self.assertNotIn(SESSION_ID, self.manager.sessions)
        try:
            record = await self.manager.session_store.get(SESSION_ID)
        except KeyError:
            return
        self.assertEqual(record.status, "closed")
        self.assertFalse(record.live)

    async def test_a_successful_create_keeps_its_directories(self) -> None:
        await self.manager.create_session()

        self.assertEqual(len(self.leftovers()), 3)


if __name__ == "__main__":
    unittest.main()
