"""The stealth scripts, run in a real Chromium.

`STEALTH_INIT_SCRIPT` was a bare arrow function. `add_init_script` injects its
source as written, so the function was defined and never called: with
STEALTH_ENABLED=true no patch ran, and the syntax gate stayed green because the
script parsed. Only a page can say whether a patch took effect, so these tests
read the patched values back from one.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager
from app.browser_scripts import apply_stealth
from tests._chromium import chromium_executable, requires_chromium
from tests.test_browser_manager_create_session import _settings

BLANK = "data:text/html,<p>x</p>"

# What each patch in STEALTH_INIT_SCRIPT claims to do, read back from the page.
BASE_PATCHES = """() => ({
  webdriver: navigator.webdriver,
  chromeRuntime: !!(window.chrome && window.chrome.runtime),
  pluginCount: navigator.plugins.length,
  languages: navigator.languages,
  hardwareConcurrency: navigator.hardwareConcurrency,
  deviceMemory: navigator.deviceMemory,
  colorDepth: screen.colorDepth,
  toDataURLPatched: !HTMLCanvasElement.prototype.toDataURL.toString().includes('[native code]'),
  glGetParameterPatched: !WebGLRenderingContext.prototype.getParameter.toString().includes('[native code]'),
})"""

NOTIFICATIONS_QUERY = """async () => {
  const result = await navigator.permissions.query({ name: 'notifications' });
  return { plainObject: Object.getPrototypeOf(result) === Object.prototype, state: result.state };
}"""


async def _launch(playwright):
    return await playwright.chromium.launch(executable_path=chromium_executable(), headless=True)


@requires_chromium
def test_apply_stealth_patches_a_real_page() -> None:
    from playwright.async_api import async_playwright

    async def run() -> tuple[dict, dict]:
        async with async_playwright() as playwright:
            browser = await _launch(playwright)
            try:
                page = await browser.new_page()
                await apply_stealth(page)
                await page.goto(BLANK)
                return await page.evaluate(BASE_PATCHES), await page.evaluate(NOTIFICATIONS_QUERY)
            finally:
                await browser.close()

    patches, notifications = asyncio.run(run())

    assert patches["webdriver"] is None, patches
    assert patches["chromeRuntime"] is True, patches
    assert patches["pluginCount"] == 3, patches
    assert patches["languages"] == ["en-US", "en"], patches
    assert patches["hardwareConcurrency"] == 8, patches
    assert patches["deviceMemory"] == 8, patches
    assert patches["colorDepth"] == 24, patches
    assert patches["toDataURLPatched"] is True, patches
    assert patches["glGetParameterPatched"] is True, patches
    assert notifications["plainObject"] is True, notifications


# A locator wait on an element that never appears fails after the default timeout.
SESSION_TIMEOUT_MS = 700


async def _stealth_and_timeout(page) -> dict:
    """Which stealth patches this page has, and how long it waits for a missing element."""
    patches = await page.evaluate(
        "() => ({ webdriver: navigator.webdriver, runtime: !!(window.chrome && window.chrome.runtime) })"
    )
    try:
        await page.locator("#never-there").wait_for()
        patches["timeout"] = None
    except Exception as exc:  # the Playwright timeout names the limit it applied
        patches["timeout"] = str(exc).splitlines()[0]
    return patches


@requires_chromium
class SessionPagesShareStealthAndTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """Stealth and the default timeout were set on the first page only, so tabs
    opened later and popups ran unpatched and waited Playwright's 30 s default."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = _settings(Path(self.tmp.name))
        settings.stealth_enabled = True
        settings.action_timeout_ms = SESSION_TIMEOUT_MS
        self.manager = BrowserManager(settings)
        self.manager.audit.append = AsyncMock()
        self.manager._persist_session = AsyncMock()
        self.manager._settle = AsyncMock()
        self.manager._maybe_provision_session_tunnel = AsyncMock()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_first_page_new_tab_and_popup_all_get_both(self) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await _launch(playwright)
            try:
                self.manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]
                session = self.manager.sessions[(await self.manager.create_session(name="tabs"))["id"]]
                try:
                    first = session.page
                    await first.goto(BLANK)
                    tab = await session.context.new_page()
                    await tab.goto(BLANK)
                    async with session.context.expect_page() as popup_info:
                        await first.evaluate("() => { window.open('about:blank'); }")
                    popup = await popup_info.value
                    results = {
                        "first page": await _stealth_and_timeout(first),
                        "new tab": await _stealth_and_timeout(tab),
                        "popup": await _stealth_and_timeout(popup),
                    }
                finally:
                    await session.context.close()
            finally:
                await browser.close()

        for name, seen in results.items():
            with self.subTest(page=name):
                self.assertIsNone(seen["webdriver"], seen)
                self.assertTrue(seen["runtime"], seen)
                self.assertIn(f"Timeout {SESSION_TIMEOUT_MS}ms", seen["timeout"] or "", seen)
