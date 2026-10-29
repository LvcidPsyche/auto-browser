"""Step 4: the paths Chromium serves without a request the pre-request gate sees, closed where step 0 measured a fix.

- Speculation-rules prefetch (decision 5, amended): the default profile's prediction preference, set when a
  launched browser is acquired, makes the prefetch ineligible, so the navigation pauses and is stopped.
- A service worker that a refused iframe registered (decision 9):
  - same-site, on the top level: Network.setBypassServiceWorker on owned pages sends the navigation to the
    network, where the gate stops it;
  - cross-site, on a popup's first navigation: storage partitioning, re-enabled in launched browsers
    (app/browser/launch_flags.py), keeps the iframe's worker from controlling the refused host's own top level.

The browser here is launched as the shadow launch launches one, with STORAGE_PARTITIONING_ON. Each test has its
positive control: the prefetch is considered (its status is reported), the worker activates and serves its own
iframe. The prefetch tests need a full Chromium: chrome-headless-shell has no prefetch service to turn off.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import Browser, async_playwright

from app.action_errors import BROWSER_ACTION_BLOCKED, BrowserActionError
from app.browser.launch_flags import STORAGE_PARTITIONING_ON
from tests._chromium import chromium_executable, requires_chromium
from tests.test_request_gate_chromium import ALLOWED, REFUSED, REFUSED_SAME_SITE, eventually, gated

_HEADLESS_SHELL = "headless-shell" in (chromium_executable() or "")
pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]
_BROWSER: dict[str, Browser] = {}
WORKER_WAIT_SECONDS = 10.0


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _shared_chromium() -> AsyncIterator[None]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=chromium_executable(), headless=True, args=[STORAGE_PARTITIONING_ON]
        )
        _BROWSER["browser"] = browser
        try:
            yield
        finally:
            _BROWSER.clear()
            await browser.close()


# ── prefetch ─────────────────────────────────────────────────────────────────


@pytest.mark.skipif(_HEADLESS_SHELL, reason="chrome-headless-shell has no prefetch service")
async def test_a_refused_prefetch_never_reaches_its_host_and_its_navigation_is_stopped(tmp_path: Path) -> None:
    async with gated(tmp_path, browser=_BROWSER["browser"]) as env:
        # The acquisition path of a launched browser: the gate, then the prefetch preference.
        await env.manager.request_gates.attach(env.browser, attach_mode=False)
        [gate_status] = env.manager.request_gates.status()["gates"]
        assert gate_status["prefetch_pref"] == 2
        target = env.server.url(REFUSED_SAME_SITE, "/prefetched")
        env.server.pages["/rules"] = (
            '<script type="speculationrules">'
            f'{{"prefetch": [{{"source": "list", "urls": ["{target}"], "eagerness": "immediate"}}]}}'
            "</script><p>rules</p>"
        )
        session_id = await env.create("/")
        page = env.manager.sessions[session_id].page
        preload = await page.context.new_cdp_session(page)
        statuses: list[str] = []
        preload.on("Preload.prefetchStatusUpdated", lambda event: statuses.append(str(event.get("prefetchStatus"))))
        await preload.send("Preload.enable")

        await page.goto(env.server.url(ALLOWED, "/rules"))
        await eventually(lambda: bool(statuses))  # the control: the rule was read and the prefetch considered
        await asyncio.sleep(1.0)
        assert env.server.hits_on(REFUSED_SAME_SITE) == [], f"the prefetch went out: {statuses}"
        assert "PrefetchNotEligiblePreloadingDisabled" in statuses, statuses

        await page.evaluate("(url) => { location.href = url; }", target)
        await eventually(lambda: env.gate().blocked_count(env.manager.sessions[session_id].gate_context_id) == 1)
        assert env.server.hits_on(REFUSED_SAME_SITE) == []


# ── service workers a refused iframe registered ──────────────────────────────

SW_JS = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (u.pathname.startsWith('SCOPEserved')) {
    e.respondWith(new Response('<!doctype html><title>SW-SERVED</title>', {headers: {'Content-Type': 'text/html'}}));
  }
});
"""


async def _worker_ready(env, frame_host: str, scope: str) -> object:
    """The top page with a refused iframe whose worker is active and serves the iframe (the control)."""
    env.server.pages[f"{scope}sw.js"] = SW_JS.replace("SCOPE", scope)
    env.server.types[f"{scope}sw.js"] = "application/javascript"
    env.server.pages[f"{scope}frame"] = (
        "<!doctype html><title>frame</title><script>"
        f"navigator.serviceWorker.register('{scope}sw.js', {{scope: '{scope}'}});</script>"
    )
    env.server.pages["/swtop"] = (
        f'<iframe id="f" src="{env.server.url(frame_host, scope + "frame")}"></iframe>'
        f'<button id="pop" onclick="window.open(\'{env.server.url(frame_host, scope + "served/pop")}\')">pop</button>'
    )
    session_id = await env.create("/swtop")
    page = env.manager.sessions[session_id].page
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WORKER_WAIT_SECONDS
    state = None
    while loop.time() < deadline:
        frame = next((f for f in page.frames if f.url.endswith(f"{scope}frame")), None)
        if frame is not None:
            state = await frame.evaluate(
                f"navigator.serviceWorker.getRegistration('{scope}').then(r => r && r.active ? r.active.state : null)"
            )
            if state == "activated":
                break
        await asyncio.sleep(0.2)
    assert state == "activated", f"the worker did not activate ({state})"
    return session_id


async def test_the_control_the_same_site_worker_serves_once_the_bypass_is_lifted(tmp_path: Path) -> None:
    """Without this the tests below could pass on a worker that serves nothing. The bypass covers the page's frames
    in its process, so the same-site iframe is bypassed too until it is lifted."""
    async with gated(tmp_path, browser=_BROWSER["browser"]) as env:
        scope = "/sw/control/"
        session_id = await _worker_ready(env, REFUSED_SAME_SITE, scope)
        page = env.manager.sessions[session_id].page
        frame = next(f for f in page.frames if f.url.endswith(f"{scope}frame"))

        await env.manager.request_gates._bypass_sessions[page].send("Network.setBypassServiceWorker", {"bypass": False})
        await frame.evaluate(f"location.href = '{scope}served/ctrl'")
        await eventually(lambda: any(f.url.endswith(f"{scope}served/ctrl") for f in page.frames))

        served = next(f for f in page.frames if f.url.endswith(f"{scope}served/ctrl"))
        assert await served.title() == "SW-SERVED"
        assert f"{scope}served/ctrl" not in env.server.pages_on(REFUSED_SAME_SITE)


async def test_a_same_site_iframes_worker_does_not_serve_the_top_level(tmp_path: Path) -> None:
    async with gated(tmp_path, browser=_BROWSER["browser"]) as env:
        scope = "/sw/same/"
        session_id = await _worker_ready(env, REFUSED_SAME_SITE, scope)
        session = env.manager.sessions[session_id]

        await session.page.evaluate(
            "(url) => { location.href = url; }", env.server.url(REFUSED_SAME_SITE, scope + "served/top")
        )

        await eventually(lambda: env.gate().blocked_count(session.gate_context_id) == 1)
        assert f"{scope}served/top" not in env.server.pages_on(REFUSED_SAME_SITE)
        assert await session.page.title() != "SW-SERVED"


async def test_a_cross_site_iframes_worker_does_not_serve_a_popups_first_navigation(tmp_path: Path) -> None:
    async with gated(tmp_path, browser=_BROWSER["browser"]) as env:
        scope = "/sw/cross/"
        session_id = await _worker_ready(env, REFUSED, scope)
        session = env.manager.sessions[session_id]

        await session.page.click("#pop")

        await eventually(lambda: env.gate().blocked_count(session.gate_context_id) == 1)
        await eventually(lambda: len(session.context.pages) == 2)
        assert f"{scope}served/pop" not in env.server.pages_on(REFUSED)
        [popup] = [page for page in session.context.pages if page is not session.page]
        assert popup.url != env.server.url(REFUSED, scope + "served/pop")


async def _refused_top_level_is_stopped(env, page, scope: str, gate_context_id: str, before: int) -> None:
    await page.evaluate("(url) => { location.href = url; }", env.server.url(REFUSED_SAME_SITE, scope + "served/top"))
    await eventually(lambda: env.gate().blocked_count(gate_context_id) == before + 1)
    assert f"{scope}served/top" not in env.server.pages_on(REFUSED_SAME_SITE)


async def test_a_tab_the_controller_opens_is_bypassed_too(tmp_path: Path) -> None:
    async with gated(tmp_path, browser=_BROWSER["browser"]) as env:
        scope = "/sw/tab/"
        session_id = await _worker_ready(env, REFUSED_SAME_SITE, scope)
        session = env.manager.sessions[session_id]

        # The tab's first navigation: bypassed before it starts, not by the page listener racing it.
        with pytest.raises(BrowserActionError) as caught:
            target = env.server.url(REFUSED_SAME_SITE, scope + "served/top")
            await env.manager.open_tab(session_id, env.server.url(ALLOWED, f"/r/302?to={target}"), True)

        assert caught.value.code == BROWSER_ACTION_BLOCKED
        assert env.gate().blocked_count(session.gate_context_id) == 1
        assert f"{scope}served/top" not in env.server.pages_on(REFUSED_SAME_SITE)


async def test_a_popup_is_bypassed_from_its_second_navigation(tmp_path: Path) -> None:
    async with gated(tmp_path, browser=_BROWSER["browser"]) as env:
        scope = "/sw/popup/"
        session_id = await _worker_ready(env, REFUSED_SAME_SITE, scope)
        session = env.manager.sessions[session_id]
        bypassed = env.manager.request_gates._bypass_sessions

        await session.page.evaluate("(url) => { window.open(url); }", env.server.url(ALLOWED, "/"))
        await eventually(lambda: len(session.context.pages) == 2)
        popup = session.context.pages[1]
        await eventually(lambda: popup in bypassed)

        await _refused_top_level_is_stopped(env, popup, scope, session.gate_context_id, 0)
