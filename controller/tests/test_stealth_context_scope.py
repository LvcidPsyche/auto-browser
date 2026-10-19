"""Stealth and the default timeout belong to the context, set before any page exists.

They were applied to the first page only, so tabs opened later and popups ran
without the stealth patches and waited Playwright's 30 s default instead of the
session's ACTION_TIMEOUT_MS. The real-browser proof is in
test_stealth_real_chromium.py; these cover the shadow-browse path, which launches a
headed browser that CI cannot, and the order of the calls.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.browser_manager import BrowserManager
from app.browser_scripts import STEALTH_INIT_SCRIPT
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage, _settings

TIMEOUT_MS = 4321


class RecordingContext(FakeContext):
    """Logs the calls that must happen before the first page is created."""

    def __init__(self, page: FakePage, log: list[str]) -> None:
        super().__init__(page)
        self.set_default_timeout = Mock(side_effect=lambda ms: log.append(f"timeout:{ms}"))
        self.add_init_script = AsyncMock(side_effect=lambda script: log.append("init_script"))
        self.new_page = AsyncMock(side_effect=lambda: log.append("new_page") or page)
        self.storage_state = AsyncMock(return_value={})


class StealthContextScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = _settings(Path(self.tmp.name))
        settings.stealth_enabled = True
        settings.shadow_browse_enabled = True
        settings.action_timeout_ms = TIMEOUT_MS
        self.manager = BrowserManager(settings)
        self.manager.audit.append = AsyncMock()
        self.manager._persist_session = AsyncMock()
        self.manager._settle = AsyncMock()
        self.manager._maybe_provision_session_tunnel = AsyncMock()
        self.log: list[str] = []

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def _browser(self) -> tuple[FakeBrowser, RecordingContext]:
        context = RecordingContext(FakePage(), self.log)
        return FakeBrowser(context), context

    def _assert_context_scoped(self, context: RecordingContext) -> None:
        context.set_default_timeout.assert_called_once_with(TIMEOUT_MS)
        context.add_init_script.assert_awaited_once_with(STEALTH_INIT_SCRIPT)
        self.assertEqual(sorted(self.log[:2]), ["init_script", f"timeout:{TIMEOUT_MS}"], self.log)
        self.assertEqual(self.log[2], "new_page", self.log)
        context.page.set_default_timeout.assert_not_called()
        context.page.add_init_script.assert_not_called()

    async def test_create_session_sets_both_on_the_context_before_the_first_page(self) -> None:
        browser, context = self._browser()
        context.page.add_init_script = AsyncMock()
        self.manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]

        await self.manager.create_session(name="scoped")

        self._assert_context_scoped(context)

    async def test_shadow_browse_sets_both_on_the_context_before_the_first_page(self) -> None:
        source_browser, source_context = self._browser()
        self.manager._acquire_session_browser = AsyncMock(return_value=(source_browser, None))  # type: ignore[method-assign]
        source = (await self.manager.create_session(name="source"))["id"]
        self.log.clear()

        headed, shadow_context = self._browser()
        shadow_context.page.add_init_script = AsyncMock()
        headed.close = AsyncMock()
        self.manager.playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=headed)))  # type: ignore[assignment]

        await self.manager.enable_shadow_browse(source)

        self._assert_context_scoped(shadow_context)

    async def test_stealth_off_sets_the_timeout_but_injects_nothing(self) -> None:
        self.manager.settings.stealth_enabled = False
        browser, context = self._browser()
        self.manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]

        await self.manager.create_session(name="plain")

        context.set_default_timeout.assert_called_once_with(TIMEOUT_MS)
        context.add_init_script.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
