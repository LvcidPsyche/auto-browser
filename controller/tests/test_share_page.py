"""What the share page shows its viewer when a frame could not be PII-scrubbed (N9).

`/share/{token}/observe` named the frame and nothing else, so a frame that screenshot scrubbing could not scrub (no
OCR, or a redaction that failed) was shown as if it had been, and one PII_SCRUB_SCREENSHOT_STRICT kept back read
"HTTP 500". These drive the page the route serves in Chromium, each request it makes answered by the real app.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import app.main as main_module
from app.browser.services import observation as observation_module
from app.routes.share import SHARE_FRAME_WITHHELD_DETAIL
from tests._chromium import chromium_executable, requires_chromium
from tests.test_share_snapshot import ShareApp

ORIGIN = "http://share.test"
SCRUBBED = {"available": True, "blocks": []}  # an OCR that ran and found nothing to paint over
NOT_SCRUBBED_NOTICE = "Not PII-scrubbed: this frame is shown as it was captured (tesseract_not_found)."
# What the page shows: its state, the scrub notice (None while it is hidden), and its frame: the path of the image
# it shows, or the text in the image's place.
SEEN = """() => {
  const notice = document.getElementById("scrub");
  const shown = document.getElementById("frame").firstElementChild;
  return {
    state: document.getElementById("state").textContent,
    notice: notice && !notice.hidden ? notice.textContent : null,
    frame: shown.tagName === "IMG" ? new URL(shown.src).pathname : shown.textContent,
  };
}"""


@requires_chromium
class SharePageTests(ShareApp):
    def setUp(self) -> None:
        super().setUp()
        # Each poll takes a frame of its own: one within the reuse window would be answered the last.
        self.stack.enter_context(patch.object(observation_module, "SHARE_FRAME_REUSE_SECONDS", 0.0))

    def view(self, *steps: Callable[[], None]) -> list[dict[str, Any]]:
        """Open the share page, and return what it shows after its first poll and after one more poll per step,
        each taken once its step has run. The page polls only when asked here, not every five seconds."""
        from playwright.async_api import async_playwright

        async def run() -> list[dict[str, Any]]:
            answering = asyncio.Lock()

            async def answer(route) -> None:
                async with answering:
                    response = await asyncio.to_thread(self.client.get, route.request.url[len(ORIGIN) :])
                await route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path=chromium_executable())
                try:
                    page = await browser.new_page()
                    page.set_default_timeout(5000)
                    await page.add_init_script("window.setInterval = () => 0;")
                    await page.route(f"{ORIGIN}/**", answer)
                    await page.goto(f"{ORIGIN}/share/tok")
                    await page.wait_for_function("document.getElementById('state').textContent !== 'Connecting'")
                    seen = [await page.evaluate(SEEN)]
                    for step in steps:
                        step()
                        await page.evaluate("refresh()")
                        seen.append(await page.evaluate(SEEN))
                    return seen
                finally:
                    await browser.close()

        return asyncio.run(run())

    def frames(self) -> list[str]:
        """The path the page asks for each frame still on disk by, oldest first."""
        return [f"/share/tok/screenshots/{path.name}" for path in sorted(self.dir.glob("*.png"))]

    def test_a_frame_that_could_not_be_scrubbed_is_shown_with_a_notice_that_says_so(self) -> None:
        ocr = self.no_ocr(strict=False)

        seen = self.view(lambda: setattr(ocr, "return_value", SCRUBBED))

        first, second = self.frames()
        self.assertEqual(
            seen,
            [
                {"state": "Live", "notice": NOT_SCRUBBED_NOTICE, "frame": first},
                # The next frame was scrubbed: the notice goes.
                {"state": "Live", "notice": None, "frame": second},
            ],
        )

    def test_a_frame_withheld_because_it_could_not_be_scrubbed_is_shown_as_withheld(self) -> None:
        self.no_ocr(strict=False)
        scrubber = main_module.manager.pii_scrubber

        seen = self.view(lambda: setattr(scrubber, "screenshot_strict", True))

        (shown,) = self.frames()
        withheld = f"Unable to refresh shared session: {SHARE_FRAME_WITHHELD_DETAIL} (HTTP 503)"
        self.assertEqual(
            seen,
            [
                {"state": "Live", "notice": NOT_SCRUBBED_NOTICE, "frame": shown},
                # PII_SCRUB_SCREENSHOT_STRICT kept the next one back: the frame before it goes, and its notice with it.
                {"state": "Error", "notice": None, "frame": withheld},
            ],
        )
