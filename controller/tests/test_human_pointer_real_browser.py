"""Real Chromium: prove the human pointer path is actually a trusted, human-shaped
pointer sequence and not a scripted shortcut a site could tell apart.

Modelled on test_tab_lanes_real_browser.py: launches headless Chromium from the
local Playwright install, builds a BrowserManager + BrowserSession by hand, and
serves pages from a local http.server. Skipped entirely if Chromium cannot
launch here.

Each test page runs its own discriminator in JavaScript and reports a verdict
to window.__verdict / window.__keydowns rather than trusting us to describe
what happened -- the point is that the page itself, which is what a real site
like Google AI Studio does, can tell a real pointer/keyboard from a scripted one.
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

HUMAN_BUTTON_PAGE = """<!doctype html>
<html><head><title>human-button</title></head>
<body>
<div style="height:300px"></div>
<button id="btn" style="width:160px;height:60px;margin-left:250px;">Click me</button>
<div style="height:600px"></div>
<script>
window.__verdict = null;
window.__moveCount = 0;
window.__sawHoverBeforeDown = false;
window.__downAt = null;
window.__downTime = null;
window.__upTrusted = null;
window.__upTime = null;

document.addEventListener('mousemove', function () { window.__moveCount++; }, true);
document.addEventListener('pointermove', function () { window.__moveCount++; }, true);

var btn = document.getElementById('btn');
btn.addEventListener('mouseover', function () { window.__sawHoverBeforeDown = true; });
btn.addEventListener('pointerover', function () { window.__sawHoverBeforeDown = true; });
btn.addEventListener('mouseenter', function () { window.__sawHoverBeforeDown = true; });

function evaluateDown(e) {
  var rect = btn.getBoundingClientRect();
  var cx = rect.left + rect.width / 2;
  var cy = rect.top + rect.height / 2;
  var insideNotCenter = (
    e.clientX >= rect.left && e.clientX <= rect.right &&
    e.clientY >= rect.top && e.clientY <= rect.bottom &&
    !(Math.abs(e.clientX - cx) < 0.5 && Math.abs(e.clientY - cy) < 0.5)
  );
  window.__downAt = {
    trusted: e.isTrusted,
    moveCountBefore: window.__moveCount,
    hoverBefore: window.__sawHoverBeforeDown,
    insideNotCenter: insideNotCenter
  };
  window.__downTime = performance.now();
}

btn.addEventListener('mousedown', function (e) { evaluateDown(e); });
btn.addEventListener('mouseup', function (e) {
  window.__upTrusted = e.isTrusted;
  window.__upTime = performance.now();
});
btn.addEventListener('click', function (e) {
  if (window.__downAt === null) { window.__verdict = 'not human'; return; }
  var gap = (window.__upTime || 0) - (window.__downTime || 0);
  var ok = (
    window.__downAt.trusted === true &&
    window.__upTrusted === true &&
    e.isTrusted === true &&
    window.__downAt.moveCountBefore >= 3 &&
    window.__downAt.hoverBefore === true &&
    window.__downAt.insideNotCenter === true &&
    gap >= 40
  );
  window.__verdict = ok ? 'human' : 'not human';
});
</script>
</body></html>"""

OVERLAY_BUTTON_PAGE = """<!doctype html>
<html><head><title>overlay-button</title></head>
<body>
<div style="position:relative;width:200px;height:80px;margin:40px;">
  <button id="covered" style="position:absolute;left:0;top:0;width:200px;height:80px;">
    Hidden target
  </button>
  <div id="overlay" style="position:absolute;left:0;top:0;width:200px;height:80px;
       background:rgba(0,0,0,0.01);"></div>
</div>
</body></html>"""

TYPE_INPUT_PAGE = """<!doctype html>
<html><head><title>type-input</title></head>
<body>
<input id="typed" type="text" />
<script>
window.__keydowns = [];
document.getElementById('typed').addEventListener('keydown', function (e) {
  window.__keydowns.push({trusted: e.isTrusted, t: performance.now(), key: e.key});
});
</script>
</body></html>"""

# A styled checkbox: the real input is invisible under its own <label> (the common
# "custom checkbox" pattern), and a field under an absolutely-positioned floating
# label that swallows pointer events.
LABELLED_CONTROLS_PAGE = """<!doctype html>
<html><head><title>labelled</title></head>
<body style="margin:40px">
<div style="position:relative;width:24px;height:24px">
  <input id="agree" type="checkbox" style="position:absolute;left:0;top:0;width:24px;height:24px;margin:0;opacity:0" />
  <label for="agree" style="position:absolute;left:0;top:0;width:24px;height:24px;background:#ccc"></label>
</div>
<div style="position:relative;width:240px;height:40px;margin-top:30px">
  <input id="email" type="text" style="position:absolute;left:0;top:0;width:240px;height:40px" />
  <div style="position:absolute;left:0;top:0;width:240px;height:40px;color:#888">Email</div>
</div>
</body></html>"""

PAGES = {
    "/labelled": LABELLED_CONTROLS_PAGE,
    "/human-button": HUMAN_BUTTON_PAGE,
    "/overlay-button": OVERLAY_BUTTON_PAGE,
    "/type-input": TYPE_INPUT_PAGE,
}


class _PageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        body = PAGES.get(self.path, "<html><body>not found</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


class HumanPointerRealBrowserTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_human_click_produces_a_trusted_human_pointer_sequence(self) -> None:
        await self.page.goto(f"{self.base}/human-button", wait_until="domcontentloaded")

        result = await self.manager.click(self.session.id, selector="#btn", pace="human")
        verdict = await self.page.evaluate("() => window.__verdict")

        self.assertEqual(verdict, "human")
        self.assertEqual(result["target"]["pointer"], "mouse")

    async def test_a_plain_scripted_click_does_not_pass_the_page_s_discriminator(self) -> None:
        await self.page.goto(f"{self.base}/human-button", wait_until="domcontentloaded")

        await self.page.evaluate("() => document.getElementById('btn').click()")
        verdict = await self.page.evaluate("() => window.__verdict")

        self.assertNotEqual(verdict, "human")

    async def test_click_on_a_fully_covered_target_raises_click_intercepted(self) -> None:
        await self.page.goto(f"{self.base}/overlay-button", wait_until="domcontentloaded")

        with self.assertRaises(BrowserActionError) as caught:
            await self.manager.click(self.session.id, selector="#covered", pace="human")

        self.assertEqual(caught.exception.code, "click_intercepted")

    async def test_human_typing_gives_trusted_keydowns_with_varied_gaps_and_correct_value(self) -> None:
        await self.page.goto(f"{self.base}/type-input", wait_until="domcontentloaded")

        await self.manager.type(
            self.session.id,
            selector="#typed",
            text="hello",
            pace="human",
            clear_first=False,
        )

        keydowns = await self.page.evaluate("() => window.__keydowns")
        value = await self.page.locator("#typed").input_value()

        self.assertEqual(len(keydowns), 5)
        self.assertTrue(all(event["trusted"] for event in keydowns))
        gaps = [b["t"] - a["t"] for a, b in zip(keydowns, keydowns[1:])]
        self.assertTrue(all(gap > 0 for gap in gaps))
        self.assertGreater(max(gaps) - min(gaps), 0)
        self.assertEqual(value, "hello")


    async def test_a_checkbox_under_its_own_label_is_clicked_through_the_label(self) -> None:
        await self.page.goto(f"{self.base}/labelled", wait_until="domcontentloaded")

        result = await self.manager.click(self.session.id, selector="#agree", pace="human")

        self.assertTrue(await self.page.locator("#agree").is_checked())
        self.assertEqual(result["target"]["pointer"], "mouse")

    async def test_a_field_under_a_floating_label_still_gets_typed_into(self) -> None:
        await self.page.goto(f"{self.base}/labelled", wait_until="domcontentloaded")

        await self.manager.type(self.session.id, selector="#email", text="a@b.co", pace="human", clear_first=False)

        self.assertEqual(await self.page.locator("#email").input_value(), "a@b.co")


if __name__ == "__main__":
    unittest.main()
