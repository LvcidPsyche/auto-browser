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
from app.stealth.fingerprint import FingerprintConfig
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


# --- the fingerprint canvas noise -------------------------------------------------

CANVAS_SESSION_ID = "canvas-noise-session"

# Draw known pixels, then read them back after `calls` more getContext('2d') calls.
READ_AFTER_CALLS = """(calls) => {
  const canvas = document.createElement('canvas');
  canvas.width = 8; canvas.height = 8;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgb(100, 100, 100)';
  ctx.fillRect(0, 0, 8, 8);
  for (let i = 0; i < calls; i++) canvas.getContext('2d');
  return Array.from(canvas.getContext('2d').getImageData(0, 0, 2, 2).data);
}"""

# Same canvas, then toDataURL `calls` times (it fetches the 2d context itself), then read.
READ_AFTER_TO_DATA_URL = """(calls) => {
  const canvas = document.createElement('canvas');
  canvas.width = 8; canvas.height = 8;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgb(100, 100, 100)';
  ctx.fillRect(0, 0, 8, 8);
  for (let i = 0; i < calls; i++) canvas.toDataURL();
  return Array.from(ctx.getImageData(0, 0, 2, 2).data);
}"""


def _expected_noised_pixels(config: FingerprintConfig) -> list[int]:
    """What one pass of the noise makes of a 2x2 block of rgb(100, 100, 100)."""
    pixels = []
    for index in range(0, 16, 4):
        noise = 1 if (config.canvas_noise_seed ^ index) & 1 else -1
        pixels += [100 + noise, 100, 100, 255]
    return pixels


@requires_chromium
def test_canvas_noise_is_applied_once_however_often_getcontext_is_called() -> None:
    from playwright.async_api import async_playwright

    config = FingerprintConfig(CANVAS_SESSION_ID, "light")

    async def run() -> dict[str, list[int]]:
        async with async_playwright() as playwright:
            browser = await _launch(playwright)
            try:
                context = await browser.new_context()
                await context.add_init_script(config.init_script())
                page = await context.new_page()
                await page.goto(BLANK)
                return {
                    "after 1 call": await page.evaluate(READ_AFTER_CALLS, 1),
                    "after 1000 calls": await page.evaluate(READ_AFTER_CALLS, 1000),
                    # Unpatched, the wrapper chain overflows the stack around 20,000 calls.
                    "after 25000 calls": await page.evaluate(READ_AFTER_CALLS, 25_000),
                }
            finally:
                await browser.close()

    seen = asyncio.run(run())
    expected = _expected_noised_pixels(config)

    assert seen["after 1 call"] == expected
    assert seen["after 1000 calls"] == expected
    assert seen["after 25000 calls"] == expected


@requires_chromium
def test_fingerprint_script_leaves_webgl_untouched() -> None:
    from playwright.async_api import async_playwright

    config = FingerprintConfig(CANVAS_SESSION_ID, "light")

    async def run() -> bool:
        async with async_playwright() as playwright:
            browser = await _launch(playwright)
            try:
                context = await browser.new_context()
                await context.add_init_script(config.init_script())
                page = await context.new_page()
                await page.goto(BLANK)
                return await page.evaluate(
                    "() => WebGLRenderingContext.prototype.getParameter.toString().includes('[native code]')"
                )
            finally:
                await browser.close()

    assert asyncio.run(run()) is True


@requires_chromium
def test_both_stealth_scripts_on_one_page_do_not_fight() -> None:
    """STEALTH_PROFILE=light adds the fingerprint script to the base one. Both patch
    navigator.webdriver, canvas reads and WebGL; neither may throw or undo the other,
    and reading a canvas back must not drift because toDataURL was called."""
    from playwright.async_api import async_playwright

    config = FingerprintConfig(CANVAS_SESSION_ID, "light")

    async def run() -> tuple[dict, list[int], list[str]]:
        async with async_playwright() as playwright:
            browser = await _launch(playwright)
            try:
                context = await browser.new_context()
                await apply_stealth(context)  # as create_session does, first
                await context.add_init_script(config.init_script())  # then the session hook
                page = await context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(BLANK)
                patches = await page.evaluate(BASE_PATCHES)
                return patches, await page.evaluate(READ_AFTER_TO_DATA_URL, 50), errors
            finally:
                await browser.close()

    patches, canvas_after_exports, errors = asyncio.run(run())

    assert errors == []
    assert patches["webdriver"] is None, patches
    assert patches["chromeRuntime"] is True, patches
    assert patches["pluginCount"] == 3, patches
    assert patches["hardwareConcurrency"] == 8, patches
    # toDataURL must leave the canvas as it found it: one noise pass on read, no more.
    assert canvas_after_exports == _expected_noised_pixels(config)
