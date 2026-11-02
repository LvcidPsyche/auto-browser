"""What re-enabling storage partitioning (decision 9) changes for sites, measured on the pinned Chromium.

The controller launches its own browsers with STORAGE_PARTITIONING_ON (app/browser/launch_flags.py), as real Chrome
runs; Playwright's default turns partitioning off. Each case runs in both, so a Chromium change in either shows up:

- a first-party login survives a storage_state save and restore either way;
- a third-party iframe keeps its cookies and localStorage across a reload of the same top-level site either way;
- what changes: a third-party iframe's localStorage is keyed to the top-level site, so the same origin opened
  top-level does not see it, and storage_state (which saves each origin's own localStorage) does not carry it:
  after a restore the iframe's localStorage is empty. Its cookies, partitioned (CHIPS) or not, round-trip.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio
from playwright.async_api import Page, async_playwright

from app.browser.launch_flags import STORAGE_PARTITIONING_ON
from tests._chromium import chromium_executable, requires_chromium

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]
TOP, THIRD = "www.site.localhost", "allowed.localhost"
FRAME_WAIT_SECONDS = 5.0
FRAME = """<!doctype html><script>
window.write = () => { document.cookie = 'tp=1; SameSite=None; Secure; path=/';
  document.cookie = 'tpc=1; SameSite=None; Secure; Partitioned; path=/'; localStorage.setItem('tp', '1'); };
window.read = () => ({cookie: document.cookie, ls: localStorage.getItem('tp')});
</script>"""
LOGIN = "<script>document.cookie = 'fp=1; path=/'; localStorage.setItem('fp', '1');</script>"
BOTH = {"cookie": "tp=1; tpc=1", "ls": "1"}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        if self.path.startswith("/frame"):
            body = FRAME
        elif self.path.startswith("/login"):
            body = LOGIN
        else:
            body = f'<iframe src="http://{THIRD}:{self.server.server_address[1]}/frame"></iframe>'
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def port() -> AsyncIterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest_asyncio.fixture(loop_scope="module", params=["partitioning_off", "partitioning_on"])
async def browser(request) -> AsyncIterator[tuple[str, object]]:
    args = [STORAGE_PARTITIONING_ON] if request.param == "partitioning_on" else []
    async with async_playwright() as playwright:
        launched = await playwright.chromium.launch(executable_path=chromium_executable(), headless=True, args=args)
        try:
            yield request.param, launched
        finally:
            await launched.close()


async def frame_of(page: Page):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FRAME_WAIT_SECONDS
    while loop.time() < deadline:
        frame = next((frame for frame in page.frames if frame.url.endswith("/frame")), None)
        if frame is not None:
            await frame.wait_for_load_state()
            return frame
        await asyncio.sleep(0.05)
    raise AssertionError("the iframe never loaded")


async def test_a_first_party_login_survives_a_storage_state_round_trip(browser, port: int) -> None:
    _mode, launched = browser
    context = await launched.new_context()
    page = await context.new_page()
    await page.goto(f"http://{TOP}:{port}/login")

    restored = await launched.new_context(storage_state=await context.storage_state())
    again = await restored.new_page()
    await again.goto(f"http://{TOP}:{port}/check")

    assert await again.evaluate("({cookie: document.cookie, ls: localStorage.getItem('fp')})") == {
        "cookie": "fp=1",
        "ls": "1",
    }


async def test_a_third_party_iframe_keeps_its_storage_across_a_reload(browser, port: int) -> None:
    _mode, launched = browser
    page = await (await launched.new_context()).new_page()
    await page.goto(f"http://{TOP}:{port}/")
    await (await frame_of(page)).evaluate("write()")

    await page.reload()

    assert await (await frame_of(page)).evaluate("read()") == BOTH


async def test_what_partitioning_changes_for_a_third_party_iframes_local_storage(browser, port: int) -> None:
    mode, launched = browser
    context = await launched.new_context()
    page = await context.new_page()
    await page.goto(f"http://{TOP}:{port}/")
    await (await frame_of(page)).evaluate("write()")
    top_level = await context.new_page()
    await top_level.goto(f"http://{THIRD}:{port}/frame")

    restored = await launched.new_context(storage_state=await context.storage_state())
    again = await restored.new_page()
    await again.goto(f"http://{TOP}:{port}/")
    after_restore = await (await frame_of(again)).evaluate("read()")

    seen_top_level = await top_level.evaluate("read()")
    if mode == "partitioning_on":
        assert seen_top_level == {"cookie": "tp=1", "ls": None}
        assert after_restore == {"cookie": "tp=1; tpc=1", "ls": None}
    else:
        assert seen_top_level == {"cookie": "tp=1", "ls": "1"}
        assert after_restore == BOTH
