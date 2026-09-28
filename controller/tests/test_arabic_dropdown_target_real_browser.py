"""Real Chromium: the Bosta-shaped area-picker failure from 2026-09-29.

An AI employee had to pick an area in an address form. The picker is a
custom dropdown/modal ("اختار المنطقة") with a search input identified only
by its placeholder text and a portal-rendered option list of plain divs (no
role="option", no tabindex -- never in the interactables list, so
click-by-text always came back `target_unresolved` even though a screenshot
showed the option plainly). Two further wrinkles from the real page:

- The employee's target text used a taa marbuta ("مدينة"); the page spells
  the same word with a haa ("مدينه"). A byte-for-byte text match never sees
  these as the same word.
- The option list is NOT nested inside the modal dialog (a separate portal
  panel appended straight to <body>), so a resolver that only looks inside
  the open dialog must still fall back to the whole page.

Modelled on test_ambiguous_targets_real_browser.py: launches headless
Chromium from the local Playwright install, builds a BrowserManager +
BrowserSession by hand, serves the fixture from a local http.server. Skipped
entirely if Chromium cannot launch here.
"""

from __future__ import annotations

import http.server
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.utils import UTC

AREA_PICKER_PAGE = """<!doctype html>
<html lang="ar" dir="rtl"><head><title>area-picker</title></head>
<body>
<div role="dialog" aria-modal="true" aria-label="اختار المنطقة"
     style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1000;
            display:flex;align-items:center;justify-content:center;">
  <div style="width:320px;background:#fff;padding:16px;">
    <h2>اختار المنطقة</h2>
    <input id="area-search" placeholder='اكتب منطقتك &quot;مدينة نصر&quot;' style="width:100%;" />
  </div>
</div>
<div id="area-options"
     style="position:fixed;top:160px;left:40px;width:320px;background:#fff;z-index:2000;">
  <div class="option" onclick="this.dataset.clicked='1'" style="cursor:pointer;padding:8px;">
    مدينه نصر - المنطقه الثامنه (مدينه نصر)
  </div>
  <div class="option" onclick="this.dataset.clicked='1'" style="cursor:pointer;padding:8px;">
    الاسكندريه
  </div>
</div>
</body></html>"""

PAGES = {"/area-picker": AREA_PICKER_PAGE}


class _PageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        body = PAGES.get(self.path, "<html><body>not found</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


class ArabicDropdownTargetRealBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            from playwright.async_api import async_playwright

            self.playwright = await async_playwright().start()
        except Exception as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"playwright unavailable: {exc}")
        try:
            self.browser = await self.playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            await self.playwright.stop()
            self.skipTest(f"chromium cannot launch here: {exc}")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        settings = Settings(
            _env_file=None,
            ARTIFACT_ROOT=str(root / "artifacts"),
            AUTH_ROOT=str(root / "auth"),
            UPLOAD_ROOT=str(root / "uploads"),
            APPROVAL_ROOT=str(root / "approvals"),
            AUDIT_ROOT=str(root / "audit"),
            WITNESS_ROOT=str(root / "witness"),
            SESSION_STORE_ROOT=str(root / "sessions"),
            ALLOWED_HOSTS="127.0.0.1",
        )
        self.manager = BrowserManager(settings)
        self.manager.playwright = self.playwright
        self.manager._persist_session = AsyncMock()  # type: ignore[method-assign]
        await self.manager.audit.startup()
        await self.manager.witness.startup()

        context = await self.browser.new_context()
        page = await context.new_page()
        artifact_dir = root / "artifacts" / "real-1"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.session = BrowserSession(
            id="real-1",
            name="real-1",
            created_at=datetime.now(UTC),
            context=context,
            page=page,
            artifact_dir=artifact_dir,
            auth_dir=root / "auth" / "real-1",
            upload_dir=root / "uploads" / "real-1",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
            browser=self.browser,
        )
        self.session.driver_epoch = self.manager._driver_epoch
        self.manager._attach_page_listeners(page, self.session)
        self.manager.sessions[self.session.id] = self.session
        self.page = page

    async def asyncTearDown(self) -> None:
        try:
            await self.session.context.close()
        except Exception:
            pass
        await self.browser.close()
        await self.playwright.stop()
        self.server.shutdown()
        self.server.server_close()
        self.tempdir.cleanup()

    async def test_type_by_placeholder_text_alone(self) -> None:
        await self.page.goto(f"{self.base}/area-picker", wait_until="domcontentloaded")

        # No id/name/aria-label matches this -- only the placeholder text does,
        # exactly as the employee saw it (quoted example included).
        result = await self.manager.type(
            self.session.id,
            selector='اكتب منطقتك "مدينة نصر"',
            text="مدينة نصر",
            pace="fast",
        )
        self.assertEqual(result["action"], "type")
        value = await self.page.eval_on_selector("#area-search", "(el) => el.value")
        self.assertEqual(value, "مدينة نصر")

    async def test_click_option_by_text_despite_taa_marbuta_vs_haa(self) -> None:
        await self.page.goto(f"{self.base}/area-picker", wait_until="domcontentloaded")

        # The employee's target spells the word with taa marbuta ("مدينة");
        # the page spells it with haa ("مدينه"). The option is a plain
        # onclick div with no role/tabindex -- never in browser.observe's
        # interactables -- and lives outside the open dialog (a separate
        # portal panel), so both the dialog-scoped and whole-page fallback
        # paths, and the Arabic normalization, are exercised here.
        result = await self.manager.click(
            self.session.id,
            selector="مدينة نصر - المنطقه الثامنه (مدينة نصر)",
            pace="fast",
        )
        self.assertEqual(result["action"], "click")
        clicked = await self.page.evaluate(
            "() => document.querySelectorAll('.option')[0].dataset.clicked === '1'"
        )
        self.assertTrue(clicked, "the Nasr City option was not the one actually clicked")

    async def test_click_does_not_pick_the_unrelated_option(self) -> None:
        await self.page.goto(f"{self.base}/area-picker", wait_until="domcontentloaded")

        result = await self.manager.click(self.session.id, selector="الاسكندريه", pace="fast")
        self.assertEqual(result["action"], "click")
        wrong_option_clicked = await self.page.evaluate(
            "() => document.querySelectorAll('.option')[0].dataset.clicked === '1'"
        )
        self.assertFalse(wrong_option_clicked, "clicked Nasr City instead of Alexandria")
        right_option_clicked = await self.page.evaluate(
            "() => document.querySelectorAll('.option')[1].dataset.clicked === '1'"
        )
        self.assertTrue(right_option_clicked)

    async def test_observe_lists_the_custom_option_divs(self) -> None:
        """The interactables snapshot itself should surface these options (by
        element_id, clickable directly) even though they carry no role or
        tabindex -- the whole point of fixing this at the source, not just at
        the click resolver."""
        await self.page.goto(f"{self.base}/area-picker", wait_until="domcontentloaded")
        observation = await self.manager.observation.observe(self.session.id)
        labels = [item.get("label", "") for item in observation["interactables"]]
        self.assertTrue(
            any("الاسكندريه" in label for label in labels),
            msg=f"custom option divs missing from interactables: {labels}",
        )

    async def test_keyboard_selection_after_type_focused_style_input(self) -> None:
        """After text lands in the search box (as type_focused delivers it),
        ArrowDown + Enter must still work as a documented selection pattern --
        `press()` dispatches to whatever the page currently has focused, not
        to a specific target, so it keeps working regardless of how the text
        got there."""
        await self.page.goto(f"{self.base}/area-picker", wait_until="domcontentloaded")
        await self.page.focus("#area-search")
        await self.manager.type_focused(self.session.id, text="مدينة نصر")
        await self.manager.press(self.session.id, "ArrowDown")
        await self.manager.press(self.session.id, "Enter")
        # No selection wiring exists on this bare fixture (no keydown handler
        # is attached) -- the assertion that matters is that press() reaches
        # the page at all after type_focused, i.e. focus was not lost.
        focused_id = await self.page.evaluate("() => document.activeElement && document.activeElement.id")
        self.assertEqual(focused_id, "area-search")


if __name__ == "__main__":
    unittest.main()
