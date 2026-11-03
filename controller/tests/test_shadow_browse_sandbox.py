"""Shadow browse launches its headed Chromium with the sandbox on.

enable_shadow_browse started a local headed Chromium with --no-sandbox and
--disable-setuid-sandbox, and Playwright's chromiumSandbox defaults to false,
so every shadow browser ran unsandboxed. The shipped controller image has no
Chromium or display, so shadow browse is a host-run feature, and on a host the
sandbox works: it was being turned off for nothing. As root, Chromium refuses to
start sandboxed, so a root controller is refused with a clear error instead of
quietly dropping the sandbox.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from app.browser.launch_flags import STORAGE_PARTITIONING_ON
from app.browser.services import sessions as sessions_module
from tests.test_request_gate_wiring import START_URL, _browser

UNSANDBOXING_SWITCHES = {"--no-sandbox", "--disable-setuid-sandbox"}


class ShadowBrowseSandboxTests(unittest.IsolatedAsyncioTestCase):
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
        self.launch = AsyncMock(return_value=_browser())
        self.harness._playwright(launch=self.launch)

    async def asyncTearDown(self) -> None:
        await self.harness.asyncTearDown()

    async def test_the_shadow_browser_is_launched_sandboxed(self) -> None:
        with patch.object(sessions_module.os, "geteuid", create=True, return_value=1000):
            result = await self.manager.enable_shadow_browse(self.source_id)

        self.assertIn("shadow_session_id", result)
        self.launch.assert_awaited_once()
        kwargs = self.launch.await_args.kwargs
        self.assertIs(kwargs.get("chromium_sandbox"), True, kwargs)
        self.assertIs(kwargs.get("headless"), False, kwargs)
        args = set(kwargs.get("args") or [])
        self.assertEqual(args & UNSANDBOXING_SWITCHES, set(), kwargs)
        self.assertIn(STORAGE_PARTITIONING_ON, args)

    async def test_a_root_controller_is_refused_before_anything_is_exported_or_launched(self) -> None:
        """Chromium needs --no-sandbox as root; refuse rather than drop the sandbox."""
        with (
            patch.object(sessions_module.os, "geteuid", create=True, return_value=0),
            self.assertRaises(RuntimeError) as caught,
        ):
            await self.manager.enable_shadow_browse(self.source_id)

        message = str(caught.exception)
        self.assertIn("root", message)
        self.assertIn("sandbox", message)
        self.source.context.storage_state.assert_not_awaited()
        self.launch.assert_not_awaited()

    async def test_a_failed_sandboxed_launch_says_what_the_sandbox_needs(self) -> None:
        # On a Linux host without unprivileged user namespaces the sandboxed launch fails;
        # the operator gets the cause and what to change, with Playwright's own error kept.
        self.launch.side_effect = RuntimeError("No usable sandbox! (Playwright's text)")
        with (
            patch.object(sessions_module.os, "geteuid", create=True, return_value=1000),
            self.assertRaises(RuntimeError) as caught,
        ):
            await self.manager.enable_shadow_browse(self.source_id)

        message = str(caught.exception)
        self.assertIn("user namespaces", message)
        self.assertIn("No usable sandbox!", message)
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    async def test_a_platform_without_geteuid_launches_sandboxed(self) -> None:
        """Windows has no geteuid and no root to refuse: the sandboxed launch goes ahead."""
        saved = getattr(os, "geteuid", None)
        if saved is not None:
            del os.geteuid
        try:
            result = await self.manager.enable_shadow_browse(self.source_id)
        finally:
            if saved is not None:
                os.geteuid = saved

        self.assertIn("shadow_session_id", result)
        self.assertIs(self.launch.await_args.kwargs.get("chromium_sandbox"), True)


if __name__ == "__main__":
    unittest.main()
