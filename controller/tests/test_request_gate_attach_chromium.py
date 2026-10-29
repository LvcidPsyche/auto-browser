"""The pre-request gate in an attach mode, on a Chrome the controller did not launch.

tests/test_request_gate_chromium.py runs on a Playwright-launched Chromium, which carries Playwright's own
--disable-features list, BlockOriginHeaderModificationOnRedirect among them, so it proves the gate's rules but not
what Chrome does in a real attach. Here chrome.exe is started as a plain subprocess (no Playwright flags) and the
controller reaches it the way an operator configures it: CDP_CONNECT_URL, then ensure_browser.

Measured on Chromium 151 in step 0 (diag_origin): without the switch, Chrome fails a cross-origin POST redirect
hop that a Fetch handler continued (net::ERR_INVALID_ARGUMENT), in every tab while the gate is attached.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Browser, async_playwright

from app.browser.request_gate import CROSS_ORIGIN_POST_REDIRECT, REDIRECT_ORIGIN_SETTING, REDIRECT_ORIGIN_SWITCH
from app.browser_manager import BrowserManager
from tests._chromium import chromium_executable, requires_chromium
from tests.test_browser_manager_create_session import _settings
from tests.test_request_gate_backstop_chromium import caught_after_the_fact
from tests.test_request_gate_chromium import (
    ALLOWED,
    ALLOWED_TOO,
    REFUSED,
    Server,
    _Handler,
    _post_form,
    eventually,
)

_HEADLESS_SHELL = "headless-shell" in (chromium_executable() or "")
pytestmark = [
    requires_chromium,
    pytest.mark.skipif(_HEADLESS_SHELL, reason="needs a full Chrome binary started as an operator would"),
    pytest.mark.asyncio(loop_scope="module"),
]

DEVTOOLS_PORT_WAIT_SECONDS = 20.0


@contextlib.contextmanager
def operator_chrome(profile: Path, *extra: str):
    """chrome.exe as an operator starts it for CDP_CONNECT_URL: headless, a debugging port, no Playwright flags."""
    profile.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            chromium_executable(),
            "--headless=new",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            *extra,
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + DEVTOOLS_PORT_WAIT_SECONDS
        while not (port_file.is_file() and port_file.read_text().strip()):
            if time.monotonic() > deadline or process.poll() is not None:
                raise RuntimeError("Chrome did not open its debugging port")
            time.sleep(0.1)
        yield f"http://127.0.0.1:{port_file.read_text().splitlines()[0]}"
    finally:
        process.kill()
        process.wait(timeout=10)


@dataclass
class Env:
    manager: BrowserManager
    server: Server
    browser: Browser

    async def create(self, path: str) -> str:
        return (await self.manager.create_session(name="t", start_url=self.server.url(ALLOWED, path)))["id"]

    def gate(self):
        return self.manager.request_gates.get(self.browser)


@contextlib.asynccontextmanager
async def attached(tmp_path: Path, *chrome_args: str, switch_declared: bool = False) -> AsyncIterator[Env]:
    state = Server()
    http = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    http.state = state  # type: ignore[attr-defined]
    state.port = http.server_address[1]
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        with operator_chrome(tmp_path / "chrome-profile", *chrome_args) as cdp_url:
            async with async_playwright() as playwright:
                settings = _settings(tmp_path)
                settings.allowed_hosts = f"{ALLOWED},{ALLOWED_TOO}"
                settings.cdp_connect_url = cdp_url
                settings.cdp_block_origin_on_redirect_disabled = switch_declared
                manager = BrowserManager(settings)
                manager._maybe_provision_session_tunnel = AsyncMock()
                manager.playwright = playwright
                browser = await manager.ensure_browser()
                try:
                    yield Env(manager=manager, server=state, browser=browser)
                finally:
                    for session_id in list(manager.sessions):
                        with contextlib.suppress(Exception):
                            await manager.close_session(session_id)
                    await manager.request_gates.release_all()
    finally:
        http.shutdown()
        http.server_close()


async def _submit(page) -> None:
    await page.click("#go")


# ── the controller's own sessions ────────────────────────────────────────────


async def test_the_gate_attaches_as_an_attach_mode_through_cdp_connect_url(tmp_path: Path) -> None:
    async with attached(tmp_path) as env:
        await env.create("/")

        gate = env.gate()
        assert gate is not None and gate.live and gate.attach_mode


async def test_a_same_origin_post_redirect_arrives_with_its_body(tmp_path: Path) -> None:
    async with attached(tmp_path) as env:
        env.server.pages["/form"] = _post_form(env, 307, env.server.url(ALLOWED, "/target"))
        session_id = await env.create("/form")

        await _submit(env.manager.sessions[session_id].page)
        await eventually(lambda: "/target" in env.server.pages_on(ALLOWED))

        [arrived] = [hit for hit in env.server.hits_on(ALLOWED) if hit.path == "/target"]
        assert (arrived.method, arrived.body) == ("POST", b"f=the-body")


async def test_a_cross_origin_post_redirect_is_stopped_with_an_error_naming_the_fix(tmp_path: Path) -> None:
    async with attached(tmp_path) as env:
        env.server.pages["/form"] = _post_form(env, 307, env.server.url(ALLOWED_TOO, "/target"))
        session_id = await env.create("/form")
        context_id = env.manager.sessions[session_id].gate_context_id

        await _submit(env.manager.sessions[session_id].page)
        await eventually(lambda: env.gate().blocked_count(context_id) == 1)

        assert env.server.hits_on(ALLOWED_TOO) == []
        [block] = env.gate().blocks(context_id)
        assert block.reason == CROSS_ORIGIN_POST_REDIRECT
        assert REDIRECT_ORIGIN_SWITCH in block.explanation and REDIRECT_ORIGIN_SETTING in block.explanation


async def test_r16_a_click_whose_post_redirect_is_stopped_succeeds_and_says_which_switch_it_needs(
    tmp_path: Path,
) -> None:
    """The agent's surface for decision 10: the click did its work; its result names the switch and the setting."""
    async with attached(tmp_path) as env:
        env.server.pages["/form"] = _post_form(env, 307, env.server.url(ALLOWED_TOO, "/target"))
        session_id = await env.create("/form")

        result = await env.manager.click(session_id, selector="#go")

        [block] = result["target"]["blocked_navigations"]
        assert block["reason"] == CROSS_ORIGIN_POST_REDIRECT
        assert REDIRECT_ORIGIN_SWITCH in block["explanation"] and REDIRECT_ORIGIN_SETTING in block["explanation"]
        assert env.server.hits_on(ALLOWED_TOO) == []


async def test_with_the_switch_declared_the_cross_origin_post_redirect_arrives_as_chrome_sends_it(
    tmp_path: Path,
) -> None:
    switch = REDIRECT_ORIGIN_SWITCH
    async with attached(tmp_path, switch, switch_declared=True) as env:
        env.server.pages["/form"] = _post_form(env, 307, env.server.url(ALLOWED_TOO, "/target"))
        session_id = await env.create("/form")

        await _submit(env.manager.sessions[session_id].page)
        await eventually(lambda: env.server.pages_on(ALLOWED_TOO) == ["/target"])

        [arrived] = [hit for hit in env.server.hits_on(ALLOWED_TOO) if hit.path == "/target"]
        # What Chrome itself sends after a cross-origin redirect: the gate changed nothing.
        assert (arrived.method, arrived.body, arrived.origin) == ("POST", b"f=the-body", "null")


# ── the operator's own tabs ──────────────────────────────────────────────────


async def test_the_operators_own_tab_is_not_gated(tmp_path: Path) -> None:
    """The tab Chrome opened itself, in its own default context, reaches a refused host."""
    async with attached(tmp_path) as env:
        await env.create("/")  # the gate is attached and owns the session's context
        [tab] = [page for context in env.browser.contexts for page in context.pages if page.url == "about:blank"][:1]
        info = await _target_info(tab)
        print(f"operator tab targetInfo: browserContextId={info.get('browserContextId')!r} type={info.get('type')!r}")

        await tab.goto(env.server.url(REFUSED, "/operator"))

        assert env.server.pages_on(REFUSED) == ["/operator"]
        assert not env.gate().owns(info.get("browserContextId"))


async def test_chrome_fails_the_operators_cross_origin_post_redirect_while_the_gate_is_attached(
    tmp_path: Path,
) -> None:
    """Documents Chrome's behaviour without the switch: the gate leaves the tab alone, Chrome fails the hop."""
    async with attached(tmp_path) as env:
        await env.create("/")
        env.server.pages["/form"] = _post_form(env, 307, env.server.url(ALLOWED_TOO, "/target"))
        [tab] = [page for context in env.browser.contexts for page in context.pages if page.url == "about:blank"][:1]
        failures: list[str] = []
        tab.on("requestfailed", lambda request: failures.append(f"{request.url} {request.failure}"))
        await tab.goto(env.server.url(ALLOWED, "/form"))

        await tab.click("#go")
        await eventually(lambda: bool(failures))

        assert env.server.hits_on(ALLOWED_TOO) == []
        assert any("ERR_INVALID_ARGUMENT" in failure for failure in failures), failures
        assert env.gate().unattributed_blocks == 0


async def _target_info(page) -> dict:
    session = await page.context.new_cdp_session(page)
    try:
        return (await session.send("Target.getTargetInfo"))["targetInfo"]
    finally:
        await session.detach()


# ── what an attach mode leaves to NAV-07 (decision 8) ────────────────────────


async def test_an_attach_modes_prefetch_reaches_the_refused_host_and_its_page_is_refused_after_it_commits(
    tmp_path: Path,
) -> None:
    """The operator's Chrome keeps its own prefetch setting, so the prefetch goes out and the navigation commits
    from it with no pause; NAV-07 still refuses the page."""
    async with attached(tmp_path) as env:
        target = env.server.url(REFUSED, "/prefetched")
        env.server.pages["/rules"] = (
            '<script type="speculationrules">'
            f'{{"prefetch": [{{"source": "list", "urls": ["{target}"], "eagerness": "immediate"}}]}}'
            "</script><p>rules</p>"
        )
        session_id = await env.create("/rules")
        session = env.manager.sessions[session_id]
        await eventually(lambda: "/prefetched" in env.server.pages_on(REFUSED))  # the control: the prefetch went out

        await session.page.evaluate("(url) => { location.href = url; }", target)

        refused = await caught_after_the_fact(env, session_id)
        assert refused == [{"url": env.server.url(REFUSED, ""), "source": "page"}]
        assert env.gate().blocked_count(session.gate_context_id) == 0, "the gate saw it: this path is not a gap"
        await eventually(lambda: session.page.url == "about:blank")


async def test_an_attach_modes_bfcache_restore_of_a_refused_page_is_refused_after_it_commits(tmp_path: Path) -> None:
    """A page that committed while its host was allowed comes back from the back/forward cache with no request."""
    async with attached(tmp_path) as env:
        session_id = await env.create("/")
        session = env.manager.sessions[session_id]
        allowed = env.manager.settings.allowed_hosts
        env.manager.settings.allowed_hosts = f"{allowed},{REFUSED}"
        try:
            await session.page.goto(env.server.url(REFUSED, "/kept"))
            await session.page.goto(env.server.url(ALLOWED, "/after"))
        finally:
            env.manager.settings.allowed_hosts = allowed

        with contextlib.suppress(Exception):
            await session.page.go_back()

        refused = await caught_after_the_fact(env, session_id)
        assert refused == [{"url": env.server.url(REFUSED, ""), "source": "page"}]
        assert env.server.pages_on(REFUSED) == ["/kept"], "the control: restored from the cache, not reloaded"
        assert env.gate().blocked_count(session.gate_context_id) == 0, "the gate saw it: this path is not a gap"
