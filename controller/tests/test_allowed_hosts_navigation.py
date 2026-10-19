"""ALLOWED_HOSTS has to hold wherever a page can end up, not only where the controller sent it.

The list was checked on the URL a caller asked for and, after an action, on the
URL the page landed on. Four places moved a session onto a disallowed host
without either check, and one more let the page get there by itself:

* create_session followed a redirect from an allowed start URL and kept the session.
* activate_tab switched to a popup that was sitting on a disallowed host.
* a blocked action's rollback (`go_back`) does nothing after `location.replace`,
  so the session stayed on the disallowed page and the 403 carried its content.
* a page that navigates itself after an action has finished (a timer) was then observed.

These run the real BrowserManager against real Chromium. No traffic leaves the
machine: every context answers each request itself, by host.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
from playwright.async_api import async_playwright

from app.browser_manager import BrowserManager
from tests._chromium import chromium_executable, requires_chromium
from tests.test_browser_manager_create_session import _settings

pytestmark = requires_chromium

SECRET = "TOP-SECRET-INTERNAL-TEXT"
EVIL_PAGE = f"<title>EVIL-TITLE</title><p>{SECRET}</p>"
ALLOWED_HOST = "localhost"
EVIL_HOST = "127.0.0.1"


def redirect(location: str) -> tuple[int, str]:
    return (302, location)


class _Handler(BaseHTTPRequestHandler):
    """Serves env.pages on `localhost` and EVIL_PAGE on `127.0.0.1`: one machine, two hostnames."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        env: Env = self.server.env  # type: ignore[attr-defined]
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        path = urlparse(self.path).path
        entry = EVIL_PAGE if host == EVIL_HOST else env.pages.get(path, "<title>allowed</title><p>hello</p>")
        if isinstance(entry, tuple):
            status, location = entry
            self.send_response(status)
            self.send_header("Location", env.fill(location))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = env.fill(entry).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@dataclass
class Env:
    """A BrowserManager browsing a local server. `localhost` is allowed; `127.0.0.1`, the same server, is not.

    Page bodies may use {ALLOWED} and {EVIL} for the two origins. A real server
    rather than context.route: Chromium does not re-route the second leg of a
    route.fulfill(302), so a redirect can only be tested against a real one.
    """

    manager: BrowserManager
    pages: dict[str, str | tuple[int, str]]
    port: int = 0
    contexts: list = field(default_factory=list)
    closed_contexts: list = field(default_factory=list)

    @property
    def allowed(self) -> str:
        return f"http://{ALLOWED_HOST}:{self.port}"

    @property
    def evil(self) -> str:
        return f"http://{EVIL_HOST}:{self.port}"

    def fill(self, text: str) -> str:
        return text.replace("{ALLOWED}", self.allowed).replace("{EVIL}", self.evil)

    async def create(self, path: str = "/") -> str:
        summary = await self.manager.create_session(name="t", start_url=f"{self.allowed}{path}")
        return summary["id"]


async def wait_for(condition: Callable[[], bool | Awaitable[bool]], *, timeout: float = 10.0) -> None:
    """Poll until `condition` holds; fail with the timeout instead of sleeping a fixed time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout}s")


class _TrackedBrowser:
    """Hands the manager the real browser, and remembers each context it opens."""

    def __init__(self, real, env: Env) -> None:
        self._real = real
        self._env = env

    async def new_context(self, **kwargs):
        context = await self._real.new_context(**kwargs)
        self._env.contexts.append(context)
        context.on("close", lambda *_: self._env.closed_contexts.append(context))
        return context


@contextlib.asynccontextmanager
async def browser_env(
    tmp_path: Path,
    pages: dict[str, str | tuple[int, str]],
    *,
    allowed_hosts: str = ALLOWED_HOST,
) -> AsyncIterator[Env]:
    settings = _settings(tmp_path)
    settings.allowed_hosts = allowed_hosts
    manager = BrowserManager(settings)
    manager._maybe_provision_session_tunnel = AsyncMock()
    env = Env(manager=manager, pages=pages)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.env = env  # type: ignore[attr-defined]
    env.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        async with async_playwright() as playwright:
            real = await playwright.chromium.launch(executable_path=chromium_executable(), headless=True)
            try:
                manager._acquire_session_browser = AsyncMock(return_value=(_TrackedBrowser(real, env), None))
                yield env
            finally:
                for context in list(env.contexts):
                    with contextlib.suppress(Exception):
                        await context.close()
                await real.close()
    finally:
        server.shutdown()
        server.server_close()


# ── H-4a: create_session ─────────────────────────────────────────────────────


async def test_create_session_refuses_a_start_url_that_redirects_off_the_list(tmp_path: Path) -> None:
    """An allowed start URL that 302s to a disallowed host must not leave a live session there."""
    async with browser_env(tmp_path, {"/redir": redirect("{EVIL}/")}) as env:
        with pytest.raises(PermissionError, match="127.0.0.1"):
            await env.create("/redir")

        assert env.manager.sessions == {}
        assert len(env.contexts) == 1
        assert env.closed_contexts == env.contexts, "the failed session's browser context was left open"


async def test_create_session_still_follows_a_redirect_between_allowed_pages(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/hop": redirect("{ALLOWED}/final"), "/final": "<p>final</p>"}) as env:
        session_id = await env.create("/hop")

        assert env.manager.sessions[session_id].page.url == f"{env.allowed}/final"


# ── H-4c: activate_tab ───────────────────────────────────────────────────────


async def open_popup(env: Env, session_id: str, url: str):
    """Open `url` in a second tab of the session, the way a page's own window.open does."""
    session = env.manager.sessions[session_id]
    async with session.context.expect_page() as opened:
        await session.page.evaluate("(url) => window.open(url)", url)
    popup = await opened.value
    await popup.wait_for_load_state()
    return popup


async def test_activate_tab_refuses_a_popup_on_a_disallowed_host(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        original = session.page
        popup = await open_popup(env, session_id, f"{env.evil}/")
        assert popup.url.startswith(env.evil)

        with pytest.raises(PermissionError, match="127.0.0.1"):
            await env.manager.activate_tab(session_id, 1)

        assert session.page is original, "the session was switched onto the disallowed tab"
        observed = await env.manager.observe(session_id, preset="text")
        assert SECRET not in str(observed)


async def test_activate_tab_still_switches_between_allowed_tabs(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/second": "<title>second</title>"}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        popup = await open_popup(env, session_id, f"{env.allowed}/second")

        await env.manager.activate_tab(session_id, 1)

        assert session.page is popup
