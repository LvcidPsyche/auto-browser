"""The stealth scripts, run in a real Chromium.

`STEALTH_INIT_SCRIPT` was a bare arrow function. `add_init_script` injects its
source as written, so the function was defined and never called: with
STEALTH_ENABLED=true no patch ran, and the syntax gate stayed green because the
script parsed. Only a page can say whether a patch took effect, so these tests
read the patched values back from one.
"""

from __future__ import annotations

import asyncio

from app.browser_scripts import apply_stealth
from tests._chromium import chromium_executable, requires_chromium

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
