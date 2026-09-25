"""Real Chromium: two employees' tabs work side by side, one tab queues.

One real session with the owner's tab plus two tabs opened (not activated)
for two employees. Tab-scoped navigations to a deliberately slow local page
run concurrently -- the pair takes about as long as ONE of them -- while a
second call on the same tab waits for the first (the pair takes ~two). The owner's active tab is
never touched. Skipped when Chromium cannot be launched here.
"""

from __future__ import annotations

import asyncio
import http.server
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser.tab_scope import current_tab_id
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.utils import UTC

SLOW_SECONDS = 1.5


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.startswith("/slow"):
            time.sleep(SLOW_SECONDS)
        extra = "<input type=file id=f>" if self.path.startswith("/upload") else ""
        body = f"<html><head><title>{self.path}</title></head><body>{self.path}{extra}</body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


class RealChromiumTabLanesTests(unittest.IsolatedAsyncioTestCase):
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

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
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
        owner_page = await context.new_page()
        await owner_page.goto(f"{self.base}/owner", wait_until="domcontentloaded")
        artifact_dir = root / "artifacts" / "real-1"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.session = BrowserSession(
            id="real-1",
            name="real-1",
            created_at=datetime.now(UTC),
            context=context,
            page=owner_page,
            artifact_dir=artifact_dir,
            auth_dir=root / "auth" / "real-1",
            upload_dir=root / "uploads" / "real-1",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
            browser=self.browser,
        )
        self.session.driver_epoch = self.manager._driver_epoch
        self.manager._attach_page_listeners(owner_page, self.session)
        self.manager.sessions[self.session.id] = self.session
        self.owner_page = owner_page

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

    async def _scoped(self, tab_id: str, call):
        token = current_tab_id.set(tab_id)
        try:
            return await call()
        finally:
            current_tab_id.reset(token)

    async def test_two_tabs_run_side_by_side_and_one_tab_queues(self) -> None:
        emad = await self.manager.open_tab(self.session.id, None, False, owner="emad")
        ziad = await self.manager.open_tab(self.session.id, None, False, owner="ziad")
        self.assertIs(self.session.page, self.owner_page, "activate=False never moves the owner's tab")

        tabs = await self.manager.list_tabs(self.session.id)
        owners = {tab["tab_id"]: tab["owner"] for tab in tabs}
        self.assertEqual(owners[emad["tab_id"]], "emad")
        self.assertEqual(owners[ziad["tab_id"]], "ziad")
        self.assertEqual([tab["active"] for tab in tabs], [True, False, False])

        # Baseline: one slow load in one tab (server delay + the action's own
        # before/after observation overhead, which varies by machine).
        started = time.monotonic()
        await self._scoped(emad["tab_id"], lambda: self.manager.navigate(self.session.id, f"{self.base}/slow-0"))
        single = time.monotonic() - started

        # Two employees, two tabs, each loading a slow page: ~max, not ~sum.
        started = time.monotonic()
        results = await asyncio.gather(
            self._scoped(emad["tab_id"], lambda: self.manager.navigate(self.session.id, f"{self.base}/slow-emad")),
            self._scoped(ziad["tab_id"], lambda: self.manager.navigate(self.session.id, f"{self.base}/slow-ziad")),
        )
        parallel = time.monotonic() - started
        self.assertLess(
            parallel, single + 0.5 * SLOW_SECONDS,
            f"ran side by side (pair {parallel:.2f}s vs one {single:.2f}s)",
        )
        self.assertEqual(results[0]["session"]["current_url"], f"{self.base}/slow-emad")
        self.assertEqual(results[1]["session"]["current_url"], f"{self.base}/slow-ziad")

        # Each landed in its own tab; the owner's tab never moved.
        tabs = {tab["tab_id"]: tab for tab in await self.manager.list_tabs(self.session.id)}
        self.assertEqual(tabs[emad["tab_id"]]["url"], f"{self.base}/slow-emad")
        self.assertEqual(tabs[ziad["tab_id"]]["url"], f"{self.base}/slow-ziad")
        self.assertIs(self.session.page, self.owner_page)
        self.assertEqual(self.owner_page.url, f"{self.base}/owner")

        # Two calls on the SAME tab queue behind each other: ~sum.
        started = time.monotonic()
        await asyncio.gather(
            self._scoped(emad["tab_id"], lambda: self.manager.navigate(self.session.id, f"{self.base}/slow-1")),
            self._scoped(emad["tab_id"], lambda: self.manager.navigate(self.session.id, f"{self.base}/slow-2")),
        )
        serial = time.monotonic() - started
        self.assertGreaterEqual(serial, 2 * SLOW_SECONDS - 0.1, f"same tab serialised ({serial:.2f}s)")
        self.assertGreater(
            serial, single + 0.9 * SLOW_SECONDS,
            f"same tab queued (pair {serial:.2f}s vs one {single:.2f}s)",
        )

        # A session-wide call waits for in-flight tab work and then runs.
        slow = asyncio.ensure_future(
            self._scoped(ziad["tab_id"], lambda: self.manager.navigate(self.session.id, f"{self.base}/slow-3"))
        )
        await asyncio.sleep(0.2)
        started = time.monotonic()
        await self.manager.activate_tab(self.session.id, 0)
        waited = time.monotonic() - started
        await slow
        self.assertGreater(waited, 0.5, "the exclusive tab switch waited for the in-flight navigation")
        self.assertFalse(self.session.lock.locked())

        # An observe through a tab reads that tab.
        observed = await self._scoped(emad["tab_id"], lambda: self.manager.observe(self.session.id, limit=5))
        self.assertEqual(observed["url"], f"{self.base}/slow-2")

    async def test_a_pushed_file_is_attached_in_the_employees_own_tab(self) -> None:
        await self.owner_page.goto(f"{self.base}/upload-owner", wait_until="domcontentloaded")
        nihad = await self.manager.open_tab(self.session.id, f"{self.base}/upload-nihad", False, owner="nihad")
        png = bytes.fromhex("89504e470d0a1a0a") + bytes(64)

        async def chunks():
            yield png

        transfer = await self.manager.file_transfers.receive_upload(
            self.session.id, filename="photo.png", chunks=chunks(), declared_length=len(png),
        )
        result = await self._scoped(
            nihad["tab_id"],
            lambda: self.manager.file_transfers.attach(self.session.id, transfer["id"], selector="#f"),
        )
        self.assertEqual(result["via"], "input")
        pages = {page.url: page for page in self.session.context.pages}
        in_tab = await pages[f"{self.base}/upload-nihad"].evaluate("document.getElementById('f').files.length")
        in_owner_tab = await self.owner_page.evaluate("document.getElementById('f').files.length")
        self.assertEqual((in_tab, in_owner_tab), (1, 0), "the file went into nihad's tab only")
        self.assertIs(self.session.page, self.owner_page)


if __name__ == "__main__":
    unittest.main()
