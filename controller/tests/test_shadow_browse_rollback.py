"""Shadow browse refuses under docker_ephemeral (NAV-06) and leaves nothing behind when it fails (NAV-05)."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.browser.services import sessions as sessions_module
from tests.test_request_gate_wiring import START_URL, _browser

SHADOW_ID = "shadow0000001"


class ShadowBrowseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from tests.test_request_gate_wiring import GateWiringTests

        self.harness = GateWiringTests()
        await GateWiringTests.asyncSetUp(self.harness)
        self.manager = self.harness.manager
        self.manager.settings.shadow_browse_enabled = True
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.harness.browser, None))  # type: ignore[method-assign]
        self.source_id = (await self.manager.create_session(name="source", start_url=START_URL))["id"]
        self.source = self.manager.sessions[self.source_id]
        self.source.context.storage_state = AsyncMock(return_value={})
        self.shadow_browser = _browser()
        self.launch = AsyncMock(return_value=self.shadow_browser)
        self.harness._playwright(launch=self.launch)
        patcher = patch.object(sessions_module, "new_session_id", return_value=SHADOW_ID)
        patcher.start()
        self.addCleanup(patcher.stop)
        geteuid = patch.object(sessions_module.os, "geteuid", create=True, return_value=1000)
        geteuid.start()
        self.addCleanup(geteuid.stop)

    async def asyncTearDown(self) -> None:
        await self.harness.asyncTearDown()

    def shadow_dirs(self) -> list[str]:
        dirs = (
            self.manager.artifacts.artifact_root / SHADOW_ID,
            self.manager.session_lifecycle.auth_root(SHADOW_ID),
            self.manager.session_lifecycle.upload_root(SHADOW_ID),
        )
        return [str(path) for path in dirs if path.exists()]

    async def test_nav06_docker_ephemeral_refuses_before_anything_is_exported_or_launched(self) -> None:
        self.manager.settings.session_isolation_mode = "docker_ephemeral"

        with self.assertRaises(RuntimeError) as caught:
            await self.manager.enable_shadow_browse(self.source_id)

        self.assertIn("docker_ephemeral", str(caught.exception))
        self.source.context.storage_state.assert_not_awaited()
        self.launch.assert_not_awaited()

    async def test_nav06_the_shadow_stays_exempt_from_the_session_limit(self) -> None:
        self.manager.settings.max_sessions = 1

        result = await self.manager.enable_shadow_browse(self.source_id)

        self.assertIn(result["shadow_session_id"], self.manager.sessions)

    async def test_nav05_a_launch_that_fails_leaves_no_directories(self) -> None:
        self.launch.side_effect = RuntimeError("no display")

        with self.assertRaises(RuntimeError):
            await self.manager.enable_shadow_browse(self.source_id)

        self.assertEqual(self.shadow_dirs(), [])

    async def test_nav05_a_failure_after_the_launch_removes_the_directories(self) -> None:
        self.shadow_browser.new_context.side_effect = RuntimeError("context refused")

        with self.assertRaises(RuntimeError):
            await self.manager.enable_shadow_browse(self.source_id)

        self.assertEqual(self.shadow_dirs(), [])
        self.shadow_browser.close.assert_awaited()

    async def test_nav05_a_cancel_after_the_launch_closes_the_browser_and_unregisters_the_session(self) -> None:
        self.shadow_browser.new_context.side_effect = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.manager.enable_shadow_browse(self.source_id)

        self.shadow_browser.close.assert_awaited()
        self.assertNotIn(SHADOW_ID, self.manager.sessions)
        self.assertEqual(self.shadow_dirs(), [])

    async def _rolled_back_after_the_persist_with(self, failing: str) -> list[str]:
        """R5C-11: the shadow fails once its active record is written, and one of _close_owned_browser's steps around
        the browser's close raises. Those steps sat outside every try, so the rollback stopped there (the directories
        and the active record stayed) and the caller got that step's error instead of its own."""

        async def persist_then_fail(session, *, status):
            # The real persist (the harness stubs _persist_session), so the shadow's active record is on disk.
            await self.manager.session_lifecycle.persist(session, status=status)
            raise RuntimeError("failed after the persist")

        self.manager._persist_session = persist_then_fail  # type: ignore[method-assign]
        gates, births = self.manager.request_gates, self.manager.window_births
        if failing == "gate release":
            gates.release = AsyncMock(side_effect=RuntimeError("the gate's detach failed"))  # type: ignore[method-assign]
        elif failing == "watcher release":
            births.release = AsyncMock(side_effect=RuntimeError("the watcher's detach failed"))  # type: ignore[method-assign]
        else:
            gates.ending = Mock(side_effect=RuntimeError("the gate could not be marked"))  # type: ignore[method-assign]

        with self.assertLogs("app.browser.services", "WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "failed after the persist"):
                await self.manager.enable_shadow_browse(self.source_id)

        self.shadow_browser.close.assert_awaited()
        self.assertNotIn(SHADOW_ID, self.manager.sessions)
        self.assertEqual(self.shadow_dirs(), [])
        record = await self.manager.session_store.get(SHADOW_ID)
        self.assertEqual((record.status, record.close_reason), ("closed", "create_failed"))
        return logs.output

    async def test_r5c_11_a_gate_release_that_raises_does_not_stop_the_rollback_or_replace_its_error(self) -> None:
        logs = await self._rolled_back_after_the_persist_with("gate release")
        self.assertIn("the gate's detach failed", "\n".join(logs))
        self.harness.browser.close.assert_not_awaited()  # the source's browser is not the one closed

    async def test_r5c_11_a_watcher_release_that_raises_does_not_stop_the_rollback_or_replace_its_error(self) -> None:
        logs = await self._rolled_back_after_the_persist_with("watcher release")
        self.assertIn("the watcher's detach failed", "\n".join(logs))

    async def test_r5c_11_a_gate_that_cannot_be_marked_ending_does_not_stop_the_rollback(self) -> None:
        logs = await self._rolled_back_after_the_persist_with("gate ending")
        self.assertIn("the gate could not be marked", "\n".join(logs))

    async def test_r5c_11_a_rollback_that_raises_still_raises_the_error_that_started_it(self) -> None:
        self.shadow_browser.new_context.side_effect = RuntimeError("context refused")
        forget = AsyncMock(side_effect=RuntimeError("the record close failed"))

        with patch.object(self.manager.session_lifecycle, "_forget_failed_session_state", forget):
            with self.assertLogs("app.browser.services", "ERROR") as logs:
                with self.assertRaisesRegex(RuntimeError, "context refused"):
                    await self.manager.enable_shadow_browse(self.source_id)

        self.assertIn("the record close failed", "\n".join(logs.output))
        self.shadow_browser.close.assert_awaited()


if __name__ == "__main__":
    unittest.main()
