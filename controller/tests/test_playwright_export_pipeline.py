"""The Playwright export, fed by the real action pipeline on a real Chromium page.

The exporter once read `details["url"]` and `details["selector"]` while the
pipeline writes `details["target"]`, so every exported line was empty. Events
built by hand cannot catch that kind of mismatch between writer and reader, so
this drives a real BrowserManager and exports what it audited. The page is
served by a local server on 127.0.0.1; nothing leaves the machine.
"""

from __future__ import annotations

import ast
import contextlib
import threading
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

from app.browser_manager import BrowserManager
from app.playwright_export import export_session_script
from tests._chromium import chromium_executable, requires_chromium
from tests.test_browser_manager_create_session import _settings

pytestmark = requires_chromium

PAGES = {
    "/login": "<title>login</title><p>sign in</p>",
    "/shop": (
        "<title>shop</title>"
        "<button id=add>Add</button><br>"
        "<input id=email><br>"
        "<input id=pw type=password><br>"
        "<select id=size><option value=small>S</option><option value=large>L</option></select><br>"
        "<button id=buy>Buy</button>"
    ),
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        body = PAGES.get(self.path, "<title>missing</title>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@contextlib.asynccontextmanager
async def browser_manager(tmp_path: Path) -> AsyncIterator[tuple[BrowserManager, str]]:
    """A BrowserManager on a real Chromium, and the local server's origin."""
    manager = BrowserManager(_settings(tmp_path))
    manager._maybe_provision_session_tunnel = AsyncMock()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    contexts = []
    try:
        async with async_playwright() as playwright:
            real = await playwright.chromium.launch(executable_path=chromium_executable(), headless=True)
            try:

                class _Browser:
                    async def new_context(self, **kwargs):
                        context = await real.new_context(**kwargs)
                        contexts.append(context)
                        return context

                manager._acquire_session_browser = AsyncMock(return_value=(_Browser(), None))
                yield manager, f"http://127.0.0.1:{server.server_address[1]}"
            finally:
                for context in contexts:
                    with contextlib.suppress(Exception):
                        await context.close()
                await real.close()
    finally:
        server.shutdown()
        server.server_close()


def body_lines(script: str) -> list[str]:
    lines = script.splitlines()
    first = lines.index("        page = context.new_page()") + 1
    last = lines.index("        context.close()")
    return [line.strip() for line in lines[first:last] if line.strip()]


async def test_a_session_driven_through_the_pipeline_exports_a_script_that_replays_it(tmp_path: Path) -> None:
    async with browser_manager(tmp_path) as (manager, origin):
        session_id = (await manager.create_session(name="export", start_url=f"{origin}/login"))["id"]

        await manager.navigate(session_id, f"{origin}/shop")
        await manager.click(session_id, selector="#add")
        await manager.type(session_id, text="a@b.co", selector="#email")
        await manager.type(session_id, text="hunter2", selector="#pw")
        await manager.press(session_id, "Tab")
        await manager.select_option(session_id, selector="#size", value="large")
        observed = await manager.observe(session_id, preset="text")
        buy = next(item for item in observed["interactables"] if item["label"] == "Buy")
        await manager.click(session_id, element_id=buy["element_id"])
        # The page the session is on now is not where it began.
        assert manager.sessions[session_id].page.url == f"{origin}/shop"

        export = await export_session_script(session_id, manager.audit)

    script = export["script"]
    note = (
        f"element_id {buy['element_id']} was tagged by auto-browser's observe() "
        "and does not exist in a replay: replace this selector"
    )
    assert body_lines(script) == [
        f"page.goto('{origin}/login')",
        "page.wait_for_load_state('domcontentloaded')",
        f"page.goto('{origin}/shop')",
        "page.locator('#add').first.click()",
        "page.locator('#email').first.clear()",
        "page.locator('#email').first.fill('a@b.co')",
        "page.locator('#pw').first.clear()",
        "page.locator('#pw').first.fill('<REDACTED>')  # text was marked sensitive and redacted",
        "page.keyboard.press('Tab')",
        "page.locator('#size').first.select_option(value='large')",
        f"page.locator('[data-operator-id=\"{buy['element_id']}\"]').first.click()  # {note}",
    ]
    assert "hunter2" not in script
    assert export["truncated"] is False
    assert export["action_count"] == 7
    compile(script, "replay.py", "exec")
    assert [type(node) for node in ast.parse(script).body] == [ast.Expr, ast.ImportFrom, ast.FunctionDef, ast.If]
