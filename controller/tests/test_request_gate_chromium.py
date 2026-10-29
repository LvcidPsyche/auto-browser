"""The pre-request gate inside the real controller, on real Chromium.

Step 0 measured the gate's rules on Chromium 151 with a prototype (classification order, ownership from
the first page, BlockedByClient, redirect hops, the cross-origin POST hop of attach modes). These
re-prove them through BrowserManager itself: a refused host receives no request at all, while iframes,
contexts the controller did not create and allowed navigations go through, and a gate that detaches
closes its sessions. The server logs every request by Host, so "no request" is checked where it matters.

`*.localhost` names resolve to loopback in Chromium, so one local server serves every host.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from playwright.async_api import Browser, async_playwright

from app.browser.request_gate import CROSS_ORIGIN_POST_REDIRECT, OFF_LIST
from app.browser_manager import BrowserManager
from tests._chromium import chromium_executable, requires_chromium
from tests.test_browser_manager_create_session import _settings

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

ALLOWED = "www.site.localhost"
# Another allowed origin, cross-site to ALLOWED.
ALLOWED_TOO = "allowed.localhost"
REFUSED = "refused.localhost"
# Refused, but same-site with ALLOWED: its iframe there is in-process, so it is no target of its own.
REFUSED_SAME_SITE = "evil.site.localhost"
_SHARED: dict[str, Browser] = {}


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _shared_chromium() -> AsyncIterator[None]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=chromium_executable(), headless=True)
        _SHARED["browser"] = browser
        try:
            yield
        finally:
            _SHARED.clear()
            await browser.close()


@dataclass
class Hit:
    host: str
    method: str
    path: str
    body: bytes
    origin: str | None = None


@dataclass
class Server:
    pages: dict[str, str] = field(default_factory=dict)
    # A path's Content-Type when it is not HTML (a service worker's script).
    types: dict[str, str] = field(default_factory=dict)
    hits: list[Hit] = field(default_factory=list)
    port: int = 0

    def url(self, host: str, path: str = "/") -> str:
        return f"http://{host}:{self.port}{path}"

    def hits_on(self, host: str) -> list[Hit]:
        """Every request the host received, the browser's own favicon fetches included."""
        return [hit for hit in self.hits if hit.host == host]

    def pages_on(self, host: str) -> list[str]:
        """The paths the host served, without the favicon a document it served makes the browser fetch."""
        return [hit.path for hit in self.hits_on(host) if hit.path != "/favicon.ico"]


class _Handler(BaseHTTPRequestHandler):
    def _serve(self) -> None:
        server: Server = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        parsed = urlparse(self.path)
        server.hits.append(
            Hit(host=host, method=self.command, path=parsed.path, body=body, origin=self.headers.get("Origin"))
        )
        if parsed.path.startswith("/r/"):
            self.send_response(int(parsed.path.removeprefix("/r/")))
            self.send_header("Location", parse_qs(parsed.query)["to"][0])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        page = server.pages.get(parsed.path, f"<title>{host}</title><p>{host}{parsed.path}</p>").encode()
        self.send_response(200)
        self.send_header("Content-Type", server.types.get(parsed.path, "text/html; charset=utf-8"))
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    do_GET = _serve  # noqa: N815 - http.server's names
    do_POST = _serve  # noqa: N815

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@dataclass
class Env:
    manager: BrowserManager
    server: Server
    browser: Browser

    async def create(self, path: str = "/") -> str:
        summary = await self.manager.create_session(name="t", start_url=self.server.url(ALLOWED, path))
        return summary["id"]

    def gate(self):
        return self.manager.request_gates.get(self.browser)


@contextlib.asynccontextmanager
async def gated(
    tmp_path: Path, *, attach_mode: bool | None = None, switch_declared: bool = False, browser: Browser | None = None
) -> AsyncIterator[Env]:
    settings = _settings(tmp_path)
    settings.allowed_hosts = f"{ALLOWED},{ALLOWED_TOO}"
    settings.cdp_block_origin_on_redirect_disabled = switch_declared
    settings.max_sessions = 4
    manager = BrowserManager(settings)
    manager._maybe_provision_session_tunnel = AsyncMock()
    browser = browser or _SHARED["browser"]
    manager._acquire_session_browser = AsyncMock(return_value=(browser, None))
    state = Server()
    http = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    http.state = state  # type: ignore[attr-defined]
    state.port = http.server_address[1]
    threading.Thread(target=http.serve_forever, daemon=True).start()
    if attach_mode is not None:
        # As an attach mode would: the gate's rules for a Chrome the controller did not launch.
        await manager.request_gates.attach(browser, attach_mode=attach_mode)
    try:
        yield Env(manager=manager, server=state, browser=browser)
    finally:
        for session_id in list(manager.sessions):
            with contextlib.suppress(Exception):
                await manager.close_session(session_id)
        await manager.request_gates.release_all()
        http.shutdown()
        http.server_close()


async def eventually(condition, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


# ── blocked before the request is sent ───────────────────────────────────────


async def test_a_start_url_that_redirects_to_a_refused_host_never_reaches_it(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        refused_target = env.server.url(REFUSED, "/landing")

        with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
            await env.create(f"/r/302?to={refused_target}")

        assert env.server.hits_on(REFUSED) == []
        assert env.server.pages_on(ALLOWED) == ["/r/302"]
        assert env.manager.sessions == {}


async def test_a_link_to_a_refused_host_is_stopped_before_it_is_sent(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        env.server.pages["/links"] = f'<a id="go" href="{env.server.url(REFUSED, "/secret?q=1")}">go</a>'
        session_id = await env.create("/links")
        session = env.manager.sessions[session_id]

        await session.page.click("#go")
        await eventually(lambda: env.gate().blocked_count(session.gate_context_id) == 1)

        assert env.server.hits_on(REFUSED) == []
        [block] = env.gate().blocks(session.gate_context_id)
        assert (block.origin, block.reason) == (env.server.url(REFUSED, ""), OFF_LIST)


async def test_a_popups_first_request_is_stopped(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]

        await session.page.evaluate("(url) => { window.open(url); }", env.server.url(REFUSED, "/popup"))
        await eventually(lambda: env.gate().blocked_count(session.gate_context_id) == 1)

        assert env.server.hits_on(REFUSED) == []
        [block] = env.gate().blocks(session.gate_context_id)
        main_target = await _target_id(session.context, session.page)
        assert block.target_id != main_target, "the popup's block was pinned on the opener"


@pytest.mark.parametrize(
    "opener",
    [
        "(url) => { window.open(url, '_blank', 'noopener'); }",
        "(url) => { const a = Object.assign(document.createElement('a'), "
        "{href: url, target: '_blank', rel: 'noopener noreferrer'}); document.body.append(a); a.click(); }",
    ],
    ids=["window_open_noopener", "link_target_blank_noopener"],
)
async def test_a_popup_with_no_opener_is_stopped_too(tmp_path: Path, opener: str) -> None:
    async with gated(tmp_path) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]

        await session.page.evaluate(opener, env.server.url(REFUSED, "/popup"))
        await eventually(lambda: env.gate().blocked_count(session.gate_context_id) == 1)

        assert env.server.hits_on(REFUSED) == []
        [block] = env.gate().blocks(session.gate_context_id)
        assert block.target_id != await _target_id(session.context, session.page), "pinned on the opener"


async def test_every_hop_of_a_redirect_chain_is_judged(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        session_id = await env.create()
        page = env.manager.sessions[session_id].page
        chain = env.server.url(ALLOWED_TOO, f"/r/302?to={env.server.url(REFUSED, '/end')}")

        with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
            await page.goto(env.server.url(ALLOWED, f"/r/302?to={chain}"))

        assert env.server.hits_on(REFUSED) == []
        assert env.server.pages_on(ALLOWED_TOO) == ["/r/302"]


# ── what the gate leaves alone ───────────────────────────────────────────────


async def test_control_an_allowed_redirect_chain_arrives(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        session_id = await env.create(f"/r/302?to={env.server.url(ALLOWED_TOO, '/end')}")

        assert env.manager.sessions[session_id].page.url == env.server.url(ALLOWED_TOO, "/end")
        assert env.server.pages_on(ALLOWED_TOO) == ["/end"]


async def test_iframes_of_a_refused_host_still_load(tmp_path: Path) -> None:
    """Out-of-process (cross-site) and in-process (same-site) iframes both continue (decision 4)."""
    async with gated(tmp_path) as env:
        env.server.pages["/frames"] = (
            f'<iframe src="{env.server.url(REFUSED, "/embed-cross")}"></iframe>'
            f'<iframe src="{env.server.url(REFUSED_SAME_SITE, "/embed-same-site")}"></iframe>'
        )
        session_id = await env.create("/frames")

        await eventually(
            lambda: len(env.server.hits_on(REFUSED)) == 1 and len(env.server.hits_on(REFUSED_SAME_SITE)) == 1
        )
        assert env.gate().blocked_count(env.manager.sessions[session_id].gate_context_id) == 0
        # The in-process frame is no target ("No target with given id"), not a failed lookup.
        assert (env.gate().unattributed_blocks, env.gate().handler_errors) == (0, 0)


async def test_an_out_of_process_iframe_navigating_again_still_loads(tmp_path: Path) -> None:
    """A cross-site iframe's first load pauses before its target exists (no target); once it is a target of
    type iframe, its next navigation is looked up as one, and continues too."""
    async with gated(tmp_path) as env:
        env.server.pages["/frame"] = f'<iframe id="f" src="{env.server.url(REFUSED, "/first")}"></iframe>'
        session_id = await env.create("/frame")
        page = env.manager.sessions[session_id].page
        await eventually(lambda: env.server.pages_on(REFUSED) == ["/first"])

        await page.evaluate("(url) => { document.getElementById('f').src = url; }", env.server.url(REFUSED, "/second"))

        await eventually(lambda: env.server.pages_on(REFUSED) == ["/first", "/second"])
        assert env.gate().blocked_count(env.manager.sessions[session_id].gate_context_id) == 0


async def test_a_context_the_controller_did_not_create_is_not_gated(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        await env.create()  # the gate is on this browser and owns that session's context
        stranger = await env.browser.new_context()
        try:
            page = await stranger.new_page()
            await page.goto(env.server.url(REFUSED, "/theirs"))

            assert env.server.pages_on(REFUSED) == ["/theirs"]
        finally:
            await stranger.close()


# ── cross-origin POST hops (decision 10) ─────────────────────────────────────


def _post_form(env: Env, code: int, target: str) -> str:
    return (
        f'<form method="post" action="/r/{code}?to={target}"><input name="f" value="the-body">'
        '<button id="go">go</button></form>'
    )


async def _submit(env: Env, session_id: str) -> None:
    await env.manager.sessions[session_id].page.click("#go")


async def test_attach_mode_stops_a_cross_origin_post_hop_and_says_why(tmp_path: Path) -> None:
    async with gated(tmp_path, attach_mode=True) as env:
        env.server.pages["/form"] = _post_form(env, 307, env.server.url(ALLOWED_TOO, "/target"))
        session_id = await env.create("/form")
        context_id = env.manager.sessions[session_id].gate_context_id

        await _submit(env, session_id)
        await eventually(lambda: env.gate().blocked_count(context_id) == 1)

        assert env.server.hits_on(ALLOWED_TOO) == []
        [block] = env.gate().blocks(context_id)
        assert block.reason == CROSS_ORIGIN_POST_REDIRECT
        assert "CDP_BLOCK_ORIGIN_ON_REDIRECT_DISABLED" in block.explanation


@pytest.mark.parametrize("code", [307, 308])
async def test_attach_mode_lets_a_same_origin_post_hop_through_with_its_body(tmp_path: Path, code: int) -> None:
    """Also proves the hop finds the URL of the request that redirected, by Chromium's own request ids."""
    async with gated(tmp_path, attach_mode=True) as env:
        env.server.pages["/form"] = _post_form(env, code, env.server.url(ALLOWED, "/target"))
        session_id = await env.create("/form")

        await _submit(env, session_id)
        await eventually(lambda: any(hit.path == "/target" for hit in env.server.hits_on(ALLOWED)))

        [arrived] = [hit for hit in env.server.hits_on(ALLOWED) if hit.path == "/target"]
        assert (arrived.method, arrived.body) == ("POST", b"f=the-body")


async def test_a_declared_switch_lets_the_cross_origin_post_hop_through(tmp_path: Path) -> None:
    async with gated(tmp_path, attach_mode=True, switch_declared=True) as env:
        env.server.pages["/form"] = _post_form(env, 307, env.server.url(ALLOWED_TOO, "/target"))
        session_id = await env.create("/form")

        await _submit(env, session_id)
        await eventually(lambda: env.server.pages_on(ALLOWED_TOO) == ["/target"])

        [arrived] = [hit for hit in env.server.hits_on(ALLOWED_TOO) if hit.path == "/target"]
        assert (arrived.method, arrived.body) == ("POST", b"f=the-body")


# ── decision 2: the gate's session detaches ──────────────────────────────────


async def test_a_gate_that_detaches_closes_its_sessions_and_the_next_one_is_gated_again(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        first = await env.create()
        lost = env.gate()

        await lost._cdp.detach()  # as if Chromium had dropped the gate's session
        await eventually(lambda: first not in env.manager.sessions)

        assert not lost.live
        second = await env.create()
        gate = env.gate()
        assert gate is not lost and gate.live
        page = env.manager.sessions[second].page
        with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
            await page.goto(env.server.url(REFUSED, "/after"))
        assert env.server.hits_on(REFUSED) == []


async def _target_id(context, page) -> str:
    session = await context.new_cdp_session(page)
    try:
        return (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
    finally:
        await session.detach()
