"""NAV-31: a password typed into a page does not come back through the raw CDP passthrough's DOM reads.

With `pierce`, DOM.getDocument returns each input's user-agent shadow root, whose text is what was typed into it.
Real Chromium, as the shape of that tree is the browser's.
"""

from __future__ import annotations

import asyncio
import json

from app.cdp.passthrough import CDPPassthrough
from tests._chromium import chromium_executable, requires_chromium

SECRET = "hunter2-typed"
PAGE = (
    "<input type='password' id='pw'><input type='text' id='plain'>"
    "<div id='host'></div><script>"
    "document.getElementById('host').attachShadow({mode: 'open'}).innerHTML = \"<input type='password' id='inner'>\";"
    "</script>"
)


@requires_chromium
def test_a_typed_password_is_blanked_in_a_pierced_dom_read_and_a_typed_text_is_not() -> None:
    from playwright.async_api import async_playwright

    async def run() -> str:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable(), headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(PAGE)
                await page.fill("#pw", SECRET)
                await page.locator("#host").locator("#inner").fill(SECRET + "-in-a-shadow-root")
                await page.fill("#plain", "typed-plain-text")
                cdp = await CDPPassthrough.from_page(page)
                return json.dumps(await cdp.raw_cdp_command("DOM.getDocument", {"depth": -1, "pierce": True}))
            finally:
                await browser.close()

    read = asyncio.run(run())

    assert SECRET not in read
    assert "typed-plain-text" in read
