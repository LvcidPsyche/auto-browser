"""Real Chromium: the "DevTools raw view" -- GET /sessions/{id}/diagnostics and the
dom-query action -- against a local fixture that behaves like a real broken site:

* a form whose submit does a real fetch() POST that comes back 422 with a JSON body
  ``{"error": "invalid area"}`` (this is where a site states the real reason);
* a console.error fired on load;
* an aria-invalid field with a role=alert message once the submit fails.

Covers, in the employee's own tab (X-Tab-Id / current_tab_id):
  * GET diagnostics returns all three (console error, failed request + body, validation
    message), plus the tab's own url/title;
  * tab scoping holds -- the owner's separate, untouched tab reports none of it;
  * dom_query finds elements by CSS and/or text, capped, without exposing JS eval;
  * a failed click (target_not_found) carries the same digest in its error payload,
    via app/actions/pipeline.py's automatic attach.

Modelled on test_find_api_keys_real_browser.py. Skipped entirely if Chromium cannot
launch here.
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
from app.browser.tab_scope import current_tab_id
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.middleware.tab_scope import is_tab_scoped_path
from app.utils import UTC

FORM_PAGE = """<!doctype html>
<html><head><title>signup form</title></head>
<body>
<script>console.error('boom load');</script>
<form id="f">
  <input id="area" name="area" aria-invalid="false">
  <div id="msg" role="alert"></div>
  <button id="submit" type="button" onclick="doSubmit()">Submit</button>
</form>
<script>
async function doSubmit() {
  const res = await fetch('/api/submit', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({area: document.getElementById('area').value}),
  });
  const data = await res.json();
  if (!res.ok) {
    document.getElementById('area').setAttribute('aria-invalid', 'true');
    document.getElementById('msg').textContent = data.error;
  }
}
</script>
</body></html>"""

PAGES = {"/form": FORM_PAGE, "/blank": "<!doctype html><html><body>nothing here</body></html>"}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        body = PAGES.get(self.path, "<html><body>not found</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/api/submit":
            body = b'{"error": "invalid area"}'
            self.send_response(422)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args) -> None:
        return


class DiagnosticsDomQueryRealBrowserTests(unittest.IsolatedAsyncioTestCase):
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

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
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

        self.context = await self.browser.new_context()
        self.owner_page = await self.context.new_page()
        await self.owner_page.goto(f"{self.base}/blank")
        artifact_dir = root / "artifacts" / "diag-1"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.session = BrowserSession(
            id="diag-1",
            name="diag-1",
            created_at=datetime.now(UTC),
            context=self.context,
            page=self.owner_page,
            artifact_dir=artifact_dir,
            auth_dir=root / "auth" / "diag-1",
            upload_dir=root / "uploads" / "diag-1",
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
        self.server.shutdown()
        self.server.server_close()
        self.tempdir.cleanup()

    async def _employee_tab(self, path: str) -> tuple[str, object]:
        opened = await self.manager.open_tab(self.session.id, f"{self.base}{path}", False, owner="emad")
        tab_id = opened["tab_id"]
        page = next(p for p in self.context.pages if p.url.endswith(path))
        page.set_default_timeout(2000)
        return tab_id, page

    async def _submit_and_wait_for_failure(self, page) -> None:
        await page.click("#submit")
        await page.wait_for_function("() => document.getElementById('area').getAttribute('aria-invalid') === 'true'")
        # The failed response's JSON body is read over its own CDP round trip
        # (see BrowserDiagnosticsService._on_response), a separate background
        # task from the page's own fetch() promise chain -- give it a moment
        # to land before asserting on it, the same eventual-consistency gap
        # NetworkInspector already has for the same reason.
        await self._wait_until_response_captured()

    async def _wait_until_response_captured(self, *, timeout: float = 2.0) -> None:
        import asyncio as _asyncio

        deadline = _asyncio.get_event_loop().time() + timeout
        while _asyncio.get_event_loop().time() < deadline:
            captured = any(
                entry.get("url", "").endswith("/api/submit")
                for buf in self.session.tab_diagnostics.values()
                for entry in buf.response_errors
            )
            if captured:
                return
            await _asyncio.sleep(0.02)

    async def _diagnostics(self, tab_id: str | None, **kwargs) -> dict:
        token = current_tab_id.set(tab_id)
        try:
            return await self.manager.get_diagnostics(self.session.id, **kwargs)
        finally:
            current_tab_id.reset(token)

    async def _dom_query(self, tab_id: str | None, **kwargs) -> dict:
        token = current_tab_id.set(tab_id)
        try:
            return await self.manager.dom_query(self.session.id, **kwargs)
        finally:
            current_tab_id.reset(token)

    def test_diagnostics_route_honours_x_tab_id(self) -> None:
        self.assertTrue(is_tab_scoped_path("GET", "/sessions/abc/diagnostics"))
        self.assertTrue(is_tab_scoped_path("POST", "/sessions/abc/actions/dom-query"))

    async def test_diagnostics_reports_console_error_failed_request_and_validation(self) -> None:
        tab_id, page = await self._employee_tab("/form")
        await self._submit_and_wait_for_failure(page)

        result = await self._diagnostics(tab_id)
        self.assertTrue(result["url"].endswith("/form"))
        self.assertEqual(result["title"], "signup form")

        console_texts = [m["text"] for m in result["console_errors"]]
        self.assertIn("boom load", console_texts)

        failed = result["failed_requests"]
        submit_entries = [f for f in failed if f.get("url", "").endswith("/api/submit")]
        self.assertEqual(len(submit_entries), 1)
        self.assertEqual(submit_entries[0]["status"], 422)
        self.assertIn("invalid area", submit_entries[0]["body"])

        kinds = {m["kind"] for m in result["validation_messages"]}
        self.assertIn("aria-invalid", kinds)
        self.assertIn("role-alert", kinds)
        messages = [m["message"] for m in result["validation_messages"] if m["kind"] == "role-alert"]
        self.assertIn("invalid area", messages)

    async def test_the_owners_tab_shows_none_of_the_employees_tab_diagnostics(self) -> None:
        tab_id, page = await self._employee_tab("/form")
        await self._submit_and_wait_for_failure(page)

        owner_view = await self._diagnostics(None)
        self.assertTrue(owner_view["url"].endswith("/blank"))
        self.assertEqual(owner_view["console_errors"], [])
        self.assertEqual(owner_view["failed_requests"], [])
        self.assertEqual(owner_view["validation_messages"], [])

        # The employee's own tab still reports its own state.
        employee_view = await self._diagnostics(tab_id)
        self.assertTrue(employee_view["console_errors"])

    async def test_dom_query_finds_the_validation_message_by_css_and_by_text(self) -> None:
        tab_id, page = await self._employee_tab("/form")
        await self._submit_and_wait_for_failure(page)

        by_css = await self._dom_query(tab_id, css="#msg")
        self.assertEqual(len(by_css["items"]), 1)
        self.assertEqual(by_css["items"][0]["text"], "invalid area")
        self.assertEqual(by_css["items"][0]["tag"], "div")
        self.assertEqual(by_css["items"][0]["role"], "alert")

        by_text = await self._dom_query(tab_id, text="invalid area")
        self.assertTrue(any(item["tag"] == "div" for item in by_text["items"]))

        # An invalid CSS selector never raises -- it comes back as a soft error.
        broken = await self._dom_query(tab_id, css=":::not-a-selector")
        self.assertEqual(broken["error"], "invalid_selector")
        self.assertEqual(broken["items"], [])

    async def test_dom_query_is_capped_and_never_the_owners_tab(self) -> None:
        tab_id, _page = await self._employee_tab("/form")
        result = await self._dom_query(tab_id, css="*", limit=3)
        self.assertLessEqual(len(result["items"]), 3)
        self.assertGreater(result["total_matched"], 3)

        owner_view = await self._dom_query(None, css="#msg")
        self.assertEqual(owner_view["items"], [], "the owner's blank page has no #msg")

    async def test_a_failed_click_carries_the_same_digest_the_employee_would_get_from_diagnostics(self) -> None:
        tab_id, page = await self._employee_tab("/form")
        await self._submit_and_wait_for_failure(page)

        token = current_tab_id.set(tab_id)
        try:
            with self.assertRaises(BrowserActionError) as caught:
                await self.manager.click(self.session.id, selector="#does-not-exist", pace="fast")
        finally:
            current_tab_id.reset(token)

        digest = caught.exception.details.get("diagnostics")
        self.assertIsNotNone(digest, "the failed click's error payload carries a diagnostics digest")
        self.assertTrue(any(m["text"] == "boom load" for m in digest["console_errors"]))
        self.assertTrue(any(f.get("status") == 422 for f in digest["failed_requests"]))
        self.assertTrue(any(m["kind"] == "aria-invalid" for m in digest["validation_messages"]))


if __name__ == "__main__":
    unittest.main()
