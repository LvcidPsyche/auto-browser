"""Real Chromium: find_api_keys finds the key Google AI Studio just created, wherever the page
puts it -- and only in the employee's own tab.

Live 2026-09-26: Emad created a key, the "API key created" dialog was on screen, and
find_api_keys returned nothing. Three faults, each covered here:
  * AI Studio has only created auth keys ("AQ." ...) since 2026-05-28; the pattern knew only
    the legacy "AIza" + 35 shape;
  * the X-Tab-Id header was ignored on this route, so it read the owner's active tab;
  * only the main frame was scanned, and a masked key (full key only on "Copy") was not
    reachable at all.

Pages come from two local servers (127.0.0.1 and localhost = two sites) so the iframe case is
a real cross-site, out-of-process frame. Skipped if Chromium cannot launch here.
"""

from __future__ import annotations

import http.server
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.browser.services import observation
from app.browser.tab_scope import current_tab_id
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.middleware.tab_scope import is_tab_scoped_path
from app.utils import UTC

# Built by concatenation so no key-shaped literal sits in the repo (release audit).
AUTH_KEY = "AQ." + "Ab8RN6" + "Lq3xVt9-" + "k2Wz_Pm7Yd4Hs1Jf6Gc0Nb5Ra8Ue3Io2"
STANDARD_KEY = "AIza" + "SyD" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
IFRAME_KEY = "AQ." + "Zz9Yy8" + "Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp9Oo8Nn7"


def _page(title: str, body: str) -> str:
    return f"<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>"


FRAME_PAGES = {
    "/key-frame": _page("frame", f"<div>Your key</div><code>{IFRAME_KEY}</code>"),
}


def main_pages(frame_base: str) -> dict[str, str]:
    masked = AUTH_KEY[:6] + "..." + AUTH_KEY[-4:]
    return {
        "/iframe-dialog": _page(
            "iframe dialog", f'<h1>API key created</h1><iframe src="{frame_base}/key-frame"></iframe>'
        ),
        "/masked-dialog": _page(
            "masked dialog",
            f"<h1>API key created</h1><span id=masked>{masked}</span>"
            f"<button id=copy onclick=\"navigator.clipboard.writeText('{AUTH_KEY}')"
            ".then(() => window.__copied = true, e => window.__copied = String(e))\">Copy</button>",
        ),
        "/copy-other": _page(
            "copy other",
            "<button id=copy onclick=\"navigator.clipboard.writeText('the owner copied this')"
            ".then(() => window.__copied = true)\">Copy</button>",
        ),
        "/input-key": _page("input", '<input id="k" readonly>'
                            f"<script>document.getElementById('k').value = '{STANDARD_KEY}';</script>"),
        "/auth-key-text": _page("text", f"<p>Key: {AUTH_KEY}.</p>"),
        "/blank": _page("blank", "<p>nothing here</p>"),
    }


def _handler(pages: dict[str, str]):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            body = pages.get(self.path, "<html><body>not found</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            return

    return Handler


def _serve(pages: dict[str, str]) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(pages))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class FindApiKeysRealBrowserTests(unittest.IsolatedAsyncioTestCase):
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

        self.frame_server = _serve(FRAME_PAGES)
        frame_base = f"http://localhost:{self.frame_server.server_address[1]}"
        self.pages = main_pages(frame_base)
        self.server = _serve(self.pages)
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
            ALLOWED_HOSTS="127.0.0.1,localhost",
        )
        self.manager = BrowserManager(settings)
        self.manager.playwright = self.playwright
        self.manager._persist_session = AsyncMock()  # type: ignore[method-assign]
        await self.manager.audit.startup()
        await self.manager.witness.startup()

        self.context = await self.browser.new_context()
        self.owner_page = await self.context.new_page()
        await self.owner_page.goto(f"{self.base}/blank")
        artifact_dir = root / "artifacts" / "keys-1"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.session = BrowserSession(
            id="keys-1",
            name="keys-1",
            created_at=datetime.now(UTC),
            context=self.context,
            page=self.owner_page,
            artifact_dir=artifact_dir,
            auth_dir=root / "auth" / "keys-1",
            upload_dir=root / "uploads" / "keys-1",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
            browser=self.browser,
        )
        self.session.driver_epoch = self.manager._driver_epoch
        self.manager._attach_page_listeners(self.owner_page, self.session)
        self.manager.sessions[self.session.id] = self.session

    async def asyncTearDown(self) -> None:
        try:
            await self.context.close()
        except Exception:
            pass
        await self.browser.close()
        await self.playwright.stop()
        for server in (self.server, self.frame_server):
            server.shutdown()
            server.server_close()
        self.tempdir.cleanup()

    async def _employee_tab(self, path: str) -> tuple[str, object]:
        opened = await self.manager.open_tab(self.session.id, f"{self.base}{path}", False, owner="emad")
        tab_id = opened["tab_id"]
        page = next(p for p in self.context.pages if p.url.endswith(path))
        return tab_id, page

    async def _find(self, tab_id: str | None) -> dict:
        token = current_tab_id.set(tab_id)
        try:
            return await self.manager.find_api_keys(self.session.id, "google")
        finally:
            current_tab_id.reset(token)

    async def _read_clipboard(self, page) -> str:
        await self.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=self.base)
        try:
            return await page.evaluate("navigator.clipboard.readText()")
        finally:
            await self.context.clear_permissions()

    def test_the_api_keys_route_honours_x_tab_id(self) -> None:
        self.assertTrue(is_tab_scoped_path("GET", "/sessions/abc/api-keys"))
        self.assertFalse(is_tab_scoped_path("POST", "/sessions/abc/api-keys"))

    async def test_auth_key_in_page_text_is_found_without_the_trailing_dot(self) -> None:
        tab_id, _page = await self._employee_tab("/auth-key-text")
        result = await self._find(tab_id)
        self.assertEqual(result["keys"], [AUTH_KEY])
        self.assertEqual(result["source"], "page")

    async def test_key_inside_a_cross_site_iframe_is_found(self) -> None:
        tab_id, page = await self._employee_tab("/iframe-dialog")
        await page.wait_for_function("() => document.querySelector('iframe').contentWindow !== null")
        frame = next(f for f in page.frames if f.url.endswith("/key-frame"))
        await frame.wait_for_selector("code")
        result = await self._find(tab_id)
        self.assertEqual(result["keys"], [IFRAME_KEY])

    async def test_key_in_an_input_value_is_found(self) -> None:
        tab_id, _page = await self._employee_tab("/input-key")
        result = await self._find(tab_id)
        self.assertEqual(result["keys"], [STANDARD_KEY])

    async def test_only_the_employee_tab_is_read_not_the_owner_active_tab(self) -> None:
        tab_id, _page = await self._employee_tab("/auth-key-text")
        self.assertIs(self.session.page, self.owner_page)
        self.assertEqual((await self._find(None))["keys"], [], "no header: the owner's tab, no key")
        self.assertEqual((await self._find(tab_id))["keys"], [AUTH_KEY])

    async def test_masked_key_is_read_from_the_clipboard_after_copy_then_cleared(self) -> None:
        tab_id, page = await self._employee_tab("/masked-dialog")
        # Headless Chromium refuses page clipboard writes without this; a real headed Chrome
        # allows a user-activated write (the employee's click on "Copy") on its own.
        await self.context.grant_permissions(["clipboard-write"], origin=self.base)
        await page.click("#copy")
        await page.wait_for_function("() => window.__copied === true")

        with patch.dict(observation.CLIPBOARD_KEY_HOSTS, {"google": ("127.0.0.1",)}):
            result = await self._find(tab_id)
        self.assertEqual(result["keys"], [AUTH_KEY])
        self.assertEqual(result["source"], "clipboard")
        self.assertEqual(await self._read_clipboard(page), "", "the clipboard is emptied after the read")

    async def test_clipboard_is_never_read_off_the_provider_site(self) -> None:
        tab_id, page = await self._employee_tab("/masked-dialog")
        await self.context.grant_permissions(["clipboard-write"], origin=self.base)
        await page.click("#copy")
        await page.wait_for_function("() => window.__copied === true")
        result = await self._find(tab_id)  # 127.0.0.1 is not aistudio.google.com
        self.assertEqual(result["keys"], [])
        self.assertEqual(await self._read_clipboard(page), AUTH_KEY, "not read, so not touched either")

    async def test_non_key_clipboard_text_is_neither_returned_nor_cleared(self) -> None:
        tab_id, page = await self._employee_tab("/copy-other")
        await self.context.grant_permissions(["clipboard-write"], origin=self.base)
        await page.click("#copy")
        await page.wait_for_function("() => window.__copied === true")
        with patch.dict(observation.CLIPBOARD_KEY_HOSTS, {"google": ("127.0.0.1",)}):
            result = await self._find(tab_id)
        self.assertEqual(result["keys"], [])
        self.assertIsNone(result["source"])
        self.assertEqual(await self._read_clipboard(page), "the owner copied this")


class AuthKeyShapeTests(unittest.TestCase):
    def test_pattern_accepts_both_google_shapes_and_nothing_loose(self) -> None:
        import re

        strict = re.compile(observation.API_KEY_PATTERNS["google"])
        for good in (AUTH_KEY, STANDARD_KEY, IFRAME_KEY, "AQ." + "a" * 60):
            self.assertTrue(strict.fullmatch(good), good[:5])
        for bad in ("AQ.", "AQ.short-token", "AQ." + ".x" * 20, "AQ." + "a" * 600, "AIza" + "x" * 20,
                    "see AQ. the next sentence", "AQ." + "a" * 40 + "."):
            self.assertFalse(strict.fullmatch(bad), bad[:12])

    def test_auth_keys_are_redacted_from_observations(self) -> None:
        text = f"API key created {AUTH_KEY} Copy; old {STANDARD_KEY}"
        clean = observation.redact_api_keys({"text_excerpt": text})["text_excerpt"]
        self.assertNotIn(AUTH_KEY, clean)
        self.assertNotIn(STANDARD_KEY, clean)
        self.assertEqual(clean.count(observation.REDACTED_API_KEY), 2)


if __name__ == "__main__":
    unittest.main()
