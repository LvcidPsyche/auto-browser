"""A response that never ends no longer holds the network inspector's read of its body (N9).

Playwright's `response.body()` waits for the end of a response, and the inspector skips only two streaming types,
so a chunked `application/x-ndjson` that a page kept open held the read, and the task that made it, for as long as
the page did. The read is now given RESPONSE_BODY_READ_SECONDS. This drives a real Chromium against a feed that
never ends, so it also shows what Playwright does with the read once it is given up: a cancelled call asks the driver
to abort it and waits for the answer, and a read that waited for ever there would never be recorded as given up.
"""

from __future__ import annotations

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

from app import network_inspector
from app.network_inspector import BODY_READ_TIMED_OUT, NetworkInspector
from tests._chromium import chromium_executable, requires_chromium

PAGE = b"<!doctype html><script>fetch('/feed').then((answer) => answer.body.getReader().read())</script>"
LINE = b'{"tick": 1}\n'
READ_SECONDS = 0.5
# Far past READ_SECONDS, so a stall on a loaded runner cannot use it up: only a read never given up runs into it.
DEADLINE_SECONDS = 20.0


class _EndlessFeed(BaseHTTPRequestHandler):
    """The page, and at /feed a chunked application/x-ndjson that sends a line every 0.1 s until the browser goes."""

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path != "/feed":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(LINE), LINE))
                self.wfile.flush()
                time.sleep(0.1)
        except OSError:
            return  # the browser went


async def _given_up(inspector: NetworkInspector) -> dict[str, Any]:
    """The feed's entry once its body is recorded as not captured. It stays in flight: the feed never ends."""
    deadline = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < deadline:
        for entry in [*inspector._pending.values(), *inspector.entries()]:
            if entry["url"].endswith("/feed") and "response_body_not_captured" in entry:
                return entry
        await asyncio.sleep(0.05)
    raise AssertionError(f"the feed's body was still being read {DEADLINE_SECONDS:g} s on")


@requires_chromium
def test_a_response_that_never_ends_frees_the_read_of_its_body() -> None:
    from playwright.async_api import async_playwright

    server = ThreadingHTTPServer(("127.0.0.1", 0), _EndlessFeed)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"

    async def run() -> dict[str, Any]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable())
            try:
                context = await browser.new_context()
                inspector = NetworkInspector("session-1")
                inspector.attach(context)
                page = await context.new_page()
                await page.goto(f"{origin}/")
                return await _given_up(inspector)
            finally:
                await browser.close()

    try:
        with patch.object(network_inspector, "RESPONSE_BODY_READ_SECONDS", READ_SECONDS):
            entry = asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()

    assert (entry["status"], entry["content_type"]) == (200, "application/x-ndjson")
    assert (entry["response_body"], entry["response_body_not_captured"]) == (None, BODY_READ_TIMED_OUT)
