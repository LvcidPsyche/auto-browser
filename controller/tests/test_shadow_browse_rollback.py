"""Shadow browse refuses under docker_ephemeral (NAV-06) and leaves nothing behind when it fails (NAV-05)."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

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


if __name__ == "__main__":
    unittest.main()
