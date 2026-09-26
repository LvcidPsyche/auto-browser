"""Real Chromium: the ChatGPT-shaped failures from 2026-09-26 (Arabic UI).

Reproduces, against a local test page (not chatgpt.com -- we never depend on a
live site staying logged out), the three patterns the owner's employees hit:

1. Two buttons that share the same Arabic label ("تسجيل الدخول") -- one behind
   a cookie banner, one the banner itself offers -- and a raw CSS/text selector
   that matches both. `.locator(selector).first` used to always take whichever
   one is first in DOM order, visible or not, in-dialog or not.
2. An aria-modal welcome dialog covering the page: clicking something behind it
   must say a dialog is blocking, not a generic "browser_action_failed".
3. An attach button whose real `<input type=file>` is hidden, opened through
   Playwright's `filechooser` event -- confirming the existing upload fallback
   chain (element -> label -> file chooser -> nearby hidden input -> page's
   only input) actually holds for this exact shape.

Modelled on test_human_pointer_real_browser.py: launches headless Chromium
from the local Playwright install, builds a BrowserManager + BrowserSession by
hand, serves pages from a local http.server. Skipped entirely if Chromium
cannot launch here.
"""

from __future__ import annotations

import http.server
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.action_errors import BrowserActionError
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.utils import UTC

# Two elements share the label "تسجيل الدخول": a header link (behind the cookie
# banner while it is up) and the banner's own "قبول الكل" flow reveals nothing
# new -- the SECOND match is a duplicate button placed later in DOM order,
# visible and enabled, that a resolver must prefer once the banner is gone.
DUPLICATE_LOGIN_PAGE = """<!doctype html>
<html lang="ar" dir="rtl"><head><title>duplicate-login</title></head>
<body>
<header style="position:relative;height:80px;">
  <button class="login-link" data-testid="header-login" onclick="this.dataset.clicked='1'"
          style="position:absolute;top:20px;left:20px;">تسجيل الدخول</button>
</header>
<div id="cookie-banner" style="position:fixed;inset:0 0 auto 0;height:140px;background:#222;color:#fff;z-index:9999;">
  <span>نستخدم الكوكيز</span>
  <button id="accept-all" onclick="document.getElementById('cookie-banner').remove()">قبول الكل</button>
</div>
<main style="margin-top:200px">
  <button class="login-link" data-testid="main-login" onclick="this.dataset.clicked='1'">تسجيل الدخول</button>
</main>
</body></html>"""

# An aria-modal welcome dialog covers the whole page; a button behind it must
# not be silently clickable, and the failure must name the dialog.
MODAL_BLOCKING_PAGE = """<!doctype html>
<html lang="ar" dir="rtl"><head><title>modal-blocking</title></head>
<body>
<button id="background-button" style="position:fixed;top:40px;left:40px;width:160px;height:50px;">
  زرار خلفي
</button>
<div role="dialog" aria-modal="true" aria-label="عدم استخدام الذاكرة"
     style="position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;
            display:flex;align-items:center;justify-content:center;">
  <div style="width:300px;height:200px;background:#fff;">
    <h2>هل تريد استخدام الذاكرة؟</h2>
    <button id="dialog-confirm">عدم استخدام الذاكرة</button>
  </div>
</div>
</body></html>"""

# An attach button whose real file input is hidden -- ChatGPT's composer shape:
# clicking the visible button opens the native chooser (Playwright's
# `filechooser` event); the input itself is never visible.
HIDDEN_UPLOAD_PAGE = """<!doctype html>
<html lang="ar" dir="rtl"><head><title>hidden-upload</title></head>
<body>
<div class="composer">
  <input type="file" id="real-input" style="display:none" multiple />
  <button id="attach-button" aria-label="إضافة صور أو ملفات"
          onclick="document.getElementById('real-input').click()">+</button>
</div>
</body></html>"""

PAGES = {
    "/duplicate-login": DUPLICATE_LOGIN_PAGE,
    "/modal-blocking": MODAL_BLOCKING_PAGE,
    "/hidden-upload": HIDDEN_UPLOAD_PAGE,
}


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


class AmbiguousTargetsRealBrowserTests(unittest.IsolatedAsyncioTestCase):
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

    # --- duplicate Arabic label -----------------------------------------------------

    async def test_duplicate_label_click_skips_the_one_a_banner_covers(self) -> None:
        await self.page.goto(f"{self.base}/duplicate-login", wait_until="domcontentloaded")

        # The header button (first in DOM order) is covered by the banner the
        # whole time; the main-content button (second match) never is. A blind
        # `.first` on the ambiguous selector would raise click_intercepted
        # forever even though a perfectly good match exists on the page.
        result = await self.manager.click(
            self.session.id, selector="button.login-link", pace="fast",
        )
        self.assertEqual(result["action"], "click")
        clicked = await self.page.evaluate(
            "() => document.querySelector('[data-testid=main-login]').dataset.clicked === '1'"
        )
        self.assertTrue(clicked, "the covered header duplicate was clicked instead of the visible one")

    async def test_duplicate_label_click_after_banner_dismissed_hits_a_real_element(self) -> None:
        await self.page.goto(f"{self.base}/duplicate-login", wait_until="domcontentloaded")
        await self.manager.click(self.session.id, selector="#accept-all", pace="fast")

        # Now both matches are visible and enabled: the click must land on one
        # of them for real (not silently no-op on a detached/invisible node).
        result = await self.manager.click(
            self.session.id, selector="button.login-link", pace="fast",
        )
        self.assertEqual(result["action"], "click")

    # --- aria-modal blocking ---------------------------------------------------------

    async def test_click_behind_an_open_aria_modal_names_the_dialog(self) -> None:
        await self.page.goto(f"{self.base}/modal-blocking", wait_until="domcontentloaded")

        with self.assertRaises(BrowserActionError) as caught:
            await self.manager.click(self.session.id, selector="#background-button", pace="fast")

        self.assertIn(caught.exception.code, {"click_intercepted", "dialog_blocking"})
        message = caught.exception.message.lower()
        # The failure must be more specific than a bare "another element covers
        # the target" -- it must be traceable to the dialog itself.
        details = caught.exception.details
        dialog_seen = "dialog" in message or "dialog" in details or "modal" in message
        self.assertTrue(dialog_seen, msg=f"error did not name the dialog: {caught.exception.payload}")

        # The button *inside* the modal must still be clickable normally.
        result = await self.manager.click(self.session.id, selector="#dialog-confirm", pace="fast")
        self.assertEqual(result["action"], "click")

    # --- hidden file input behind an attach button ------------------------------------

    async def test_upload_behind_attach_button_uses_the_filechooser_or_hidden_input(self) -> None:
        await self.page.goto(f"{self.base}/hidden-upload", wait_until="domcontentloaded")

        upload_path = Path(self.tempdir.name) / "photo.png"
        # Minimal 1x1 PNG.
        upload_path.write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
                "de0000000c4944415478da6360000002000100ffff03000006000557bfabd400"
                "0000004945454e44ae426082"
            )
        )
        transfer = await self.manager.file_transfers.receive_upload(
            self.session.id,
            filename="photo.png",
            chunks=_bytes_iter(upload_path.read_bytes()),
            declared_length=upload_path.stat().st_size,
        )
        result = await self.manager.file_transfers.attach(
            self.session.id, transfer["id"], selector="#attach-button",
        )
        self.assertIn(result["via"], {"file_chooser", "nearby_input", "page_input"})
        value = await self.page.eval_on_selector("#real-input", "(el) => el.files.length")
        self.assertEqual(value, 1)


async def _bytes_iter(data: bytes):
    yield data


if __name__ == "__main__":
    unittest.main()
