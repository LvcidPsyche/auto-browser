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

    async def test_w4_23_the_rollback_removes_the_directories_in_a_worker_thread(self) -> None:
        # shutil.rmtree ran on the event loop, so rolling back a session with large directories stalled every request.
        import asyncio

        from app.browser.services import session_creation

        self.manager._acquire_session_browser = AsyncMock(side_effect=RuntimeError("no browser"))  # type: ignore[method-assign]
        real_to_thread = asyncio.to_thread
        offloaded: list[str] = []

        async def to_thread(function, /, *args, **kwargs):
            offloaded.append(getattr(function, "__name__", repr(function)))
            return await real_to_thread(function, *args, **kwargs)

        with patch.object(session_creation.asyncio, "to_thread", to_thread, create=True):
            with self.assertRaises(RuntimeError):
                await self.manager.create_session()

        self.assertIn("remove_session_dirs", offloaded)
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

    async def test_w5_6_a_cancel_during_the_rollback_still_closes_the_record_and_removes_the_directories(self) -> None:
        # The directory removal is awaited before the closed record is written: a second cancel there (a shutdown)
        # left a record the create had already persisted reading active.
        import asyncio
        import threading

        from app.browser.services.sessions import BrowserSessionService

        self.manager.witness_bridge.record_session_receipt = AsyncMock(side_effect=RuntimeError("witness down"))  # type: ignore[method-assign]
        started, go_on = threading.Event(), threading.Event()
        self.addCleanup(go_on.set)
        real = BrowserSessionService.remove_session_dirs

        def gated(service, session_id):
            started.set()
            go_on.wait(10)
            real(service, session_id)

        with patch.object(BrowserSessionService, "remove_session_dirs", gated):
            task = asyncio.create_task(self.manager.create_session())
            self.assertTrue(await asyncio.to_thread(started.wait, 10), "the rollback never removed the directories")
            self.assertEqual((await self.manager.session_store.get(SESSION_ID)).status, "active")
            task.cancel()
            await asyncio.sleep(0.05)
            go_on.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        record = await self.manager.session_store.get(SESSION_ID)
        self.assertEqual(record.status, "closed")
        self.assertEqual(record.close_reason, "create_failed")
        self.assertFalse(record.live)
        self.assertEqual(self.leftovers(), [])

    async def test_a_successful_create_keeps_its_directories(self) -> None:
        await self.manager.create_session()

        self.assertEqual(len(self.leftovers()), 3)


if __name__ == "__main__":
    unittest.main()
