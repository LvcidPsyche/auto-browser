"""A window a refused page opens is closed, not leaked, however its first navigation behaves — in real Chromium.

The window-birth watcher (app/browser/window_births.py) judges a page target at its CDP birth, so a window a
refused page opens is refused whatever its first navigation does: a 204/205, a hang, javascript:void(0), a
blocked top-level data: URL, or a blank the opener writes into. Playwright reports such a window only once it
commits, long after the agent's navigate has cleared the opener, so the old opener-at-report verdict let it
through. Now the window is born refused and, because Playwright never reported it, closed through CDP when the
session stops counting: its planted script cannot then send it (or the agent's own tab) to an allowed URL
carrying the secret.

These run the real BrowserManager against real Chromium with PRE_REQUEST_BLOCKING off (browser_env), the
no-gate path where the gap was a full bypass. `localhost` is allowed; `127.0.0.1`, the same server, is not.
A window closed before it commits never reaches `/landing`, so the server's `/landing` count is the proof it
was closed, and the SECRET never reaches list_tabs, observe or the console.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from pathlib import Path

import pytest

from app.browser.window_births import WindowBirths, WindowBirthWatcher, WindowBirthWatcherUnavailable
from tests import test_allowed_hosts_navigation as nav
from tests._background import pending
from tests._chromium import requires_chromium
from tests.test_allowed_hosts_channels import SCRIPT_DONE, SCRIPT_TIMEOUT_SECONDS, _NoContentHandler
from tests.test_allowed_hosts_navigation import (  # noqa: F401 - _shared_chromium is the module's Chromium fixture
    SECRET,
    _shared_chromium,
    browser_env,
)
from tests.test_allowed_hosts_popups import settle
from tests.test_allowed_hosts_sessionwide import past_the_grace

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

# A 1x1 GIF. An image loads cross-origin without CORS, so a script on the refused origin can tell when an
# allowed-host resource is ready (a fetch would be opaque). /released is a 404 until the test sets RELEASED.
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
RELEASED = threading.Event()
HANG_RELEASE = threading.Event()
LANDING: list[str] = []
WINDOW_TIMEOUT_SECONDS = 10.0

# The planted window's poll script, as a JS string the opener writes into the window. Once /released loads it
# sends __TARGET__ to an allowed URL whose query carries what the opener read. {ALLOWED} is filled by Env.fill
# when the page is served. __OPEN__ is the window.open() argument; __TARGET__ is `location` (the window itself,
# a list_tabs/observe leak) or `opener.location` (the agent's own tab, the rollback residual). __EXTRA__ runs
# after the window is open, before its script is written.
_PLANT = (
    "const s = document.body.innerText;"
    "const w = window.open(__OPEN__);"
    "await new Promise((r) => setTimeout(r, 300));"
    "__EXTRA__"
    "w.document.write('<script>const S = ' + JSON.stringify(s) + ';(function poll() {"
    " const i = new Image();"
    ' i.onload = () => { __TARGET__.href = "{ALLOWED}/landing?d=" + encodeURIComponent(S) };'
    " i.onerror = () => setTimeout(poll, 100);"
    " i.src = \"{ALLOWED}/released?\" + Date.now(); })()</scr' + 'ipt>');"
    "w.document.close();"
)


def plant(open_arg: str, *, target: str = "location", extra: str = "") -> str:
    return _PLANT.replace("__OPEN__", open_arg).replace("__TARGET__", target).replace("__EXTRA__", extra)


def opener_script(body: str) -> str:
    # Concatenation, not an f-string: the body keeps its literal {ALLOWED} for Env.fill at serve time.
    return "(async () => { " + body + " })().finally(() => console.log('" + SCRIPT_DONE + "'))"


class _Handler(_NoContentHandler):
    """The 204 handler, plus /no-content-205, /hang, /released (404 then a GIF) and a /landing counter."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        if self.path.startswith("/no-content-205"):
            self.send_response(205)
            self.end_headers()
            return
        if self.path.startswith("/hang"):
            HANG_RELEASE.wait(WINDOW_TIMEOUT_SECONDS)
            self.send_response(204)
            self.end_headers()
            return
        if self.path.startswith("/released"):
            if not RELEASED.is_set():
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.send_header("Content-Length", str(len(GIF)))
            self.end_headers()
            self.wfile.write(GIF)
            return
        if self.path.startswith("/landing"):
            LANDING.append(self.path)
        super().do_GET()


def install(monkeypatch, body: str) -> None:
    monkeypatch.setattr(nav, "_Handler", _Handler)
    monkeypatch.setattr(nav, "EVIL_PAGE", nav.EVIL_PAGE + "<script>" + opener_script(body) + "</script>")
    RELEASED.clear()
    HANG_RELEASE.clear()
    LANDING.clear()


async def drive_refused_window(env: nav.Env, session_id: str) -> None:
    """Drive the session's page to the refused host, where its planted script opens and seeds the window."""
    session = env.manager.sessions[session_id]
    done = asyncio.Event()
    session.context.on("console", lambda m: done.set() if m.text == SCRIPT_DONE else None)
    async with session.lock:
        await session.page.evaluate("(url) => { location.href = url }", f"{env.evil}/")
        await asyncio.wait_for(done.wait(), timeout=SCRIPT_TIMEOUT_SECONDS)
    assert await settle(lambda: session.page.url == "about:blank")


# ── the window's first navigation commits nothing, or makes no request at all ──

FIRST_NAVIGATIONS = {
    "204": "'{ALLOWED}/no-content'",
    "205": "'{ALLOWED}/no-content-205'",
    "hang": "'{ALLOWED}/hang'",
    "javascript": "'javascript:void(0)'",
    "data": "'data:text/html,<p>x</p>'",
    "blank-written-into": "''",
}


@pytest.mark.parametrize("first_nav", sorted(FIRST_NAVIGATIONS), ids=sorted(FIRST_NAVIGATIONS))
async def test_a_window_a_refused_page_opens_is_closed_whatever_its_first_navigation(
    tmp_path: Path, monkeypatch, first_nav: str
) -> None:
    """Across a 204, a 205, a hang, javascript:void(0), a blocked data: URL and a blank written into: the
    window is born refused and, never reported, closed on the agent's navigate before it can leak."""
    install(monkeypatch, plant(FIRST_NAVIGATIONS[first_nav]))
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]

        await drive_refused_window(env, session_id)
        await env.manager.navigate(session_id, f"{env.allowed}/")
        assert not env.manager._page_refused(session.page)
        await past_the_grace(monkeypatch)
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)

        # The window is closed now. Releasing it does nothing: nothing reaches /landing, nothing it held
        # reaches the agent.
        RELEASED.set()
        HANG_RELEASE.set()
        await asyncio.sleep(0.8)
        tabs = await env.manager.list_tabs(session_id)
        assert LANDING == [], f"a window that should have been closed still navigated to /landing: {LANDING}"
        assert SECRET not in json.dumps(tabs), tabs
        assert SECRET not in json.dumps(session.console_messages), session.console_messages
        assert not env.manager._page_refused(session.page)


async def test_control_a_window_an_allowed_page_opens_is_read_once_it_commits(tmp_path: Path, monkeypatch) -> None:
    """The same window, its 204 and its planted script, from an allowed page with no refused page in the
    session: it is reported where the script sends it and read. So the test above is not passing on a window
    that never got there, and a clean page's popup is not closed (no over-refusal)."""
    install(monkeypatch, "")  # nothing added to the evil page; the allowed page runs the plant itself
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        reported: list = []
        session.context.on("page", lambda p: reported.append(p))

        await session.page.evaluate("async () => { " + env.fill(plant("'{ALLOWED}/no-content'")) + " }")
        RELEASED.set()
        assert await settle(lambda: len(reported) == 1, timeout=WINDOW_TIMEOUT_SECONDS), reported
        window = reported[0]
        assert await settle(lambda: "/landing" in window.url, timeout=WINDOW_TIMEOUT_SECONDS), window.url
        assert not env.manager._page_refused(window)


# ── a detached iframe in the window must not poison the session ────────────────


async def test_a_detached_iframe_in_the_window_makes_no_entry_and_does_not_leak(tmp_path: Path, monkeypatch) -> None:
    """The refused page writes an iframe into its window and detaches it. The iframe is not a page target, so
    it makes no window entry (the rejected design recorded its frameless navigation and then refused every
    later popup for good); the window itself is still refused and does not leak."""
    extra = (
        "const f = w.document.createElement('iframe'); f.src = '{ALLOWED}/no-content';"
        "(w.document.body || w.document.documentElement).appendChild(f);"
        "await new Promise((r) => setTimeout(r, 100)); f.remove();"
    )
    install(monkeypatch, plant("''", extra=extra))
    async with browser_env(tmp_path, {"/ok": "<title>ok</title><p>fine</p>"}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        watcher = env.manager.window_births._get(session.browser)

        await drive_refused_window(env, session_id)
        await env.manager.navigate(session_id, f"{env.allowed}/ok")
        await past_the_grace(monkeypatch)
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)
        RELEASED.set()
        await asyncio.sleep(0.5)

        assert LANDING == [], LANDING
        # One entry for the opener and one for the window; the detached iframe added none.
        assert len(watcher._births) <= 3, [b.target_id for b in watcher._births.values()]
        tabs = await env.manager.list_tabs(session_id)
        assert SECRET not in json.dumps(tabs), tabs
        # The window a refused page opened is refused wherever it ended up.
        windows = [p for p in session.context.pages if p is not session.page]
        assert windows and all(env.manager._page_refused(p) for p in windows), [p.url for p in windows]


# ── a navigation flood leaves bounded state ────────────────────────────────────


async def test_a_navigation_flood_from_an_unreported_window_leaves_bounded_state(tmp_path: Path, monkeypatch) -> None:
    """The window loops navigations that commit nothing every 10 ms. State is one entry per live target, so it
    stays bounded (the rejected design recorded one pinned Request per navigation, about 100 a second)."""
    body = (
        "const w = window.open('');"
        "await new Promise((r) => setTimeout(r, 200));"
        "w.document.write('<script>let i = 0; setInterval(() => {"
        " location.href = \"{ALLOWED}/no-content?\" + (i++); }, 10)</scr' + 'ipt>');"
        "w.document.close();"
    )
    install(monkeypatch, body)
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        watcher = env.manager.window_births._get(session.browser)

        await drive_refused_window(env, session_id)
        await asyncio.sleep(2.0)  # let the flood run

        births = len(watcher._births)
        assert births <= 4, f"state grew under the navigation flood: {births} entries"


# ── the opener navigating the agent's own tab after a rollback (go_back) ───────


def _live_born_refused(watcher) -> list[str]:
    """Born-refused windows Playwright never reported and the sweep has not closed: the residual."""
    return [b.target_id for b in watcher._births.values() if b.born_refused and not b.reported and not b.closed]


async def test_a_window_a_refused_page_opened_is_closed_by_a_blocked_actions_rollback(
    tmp_path: Path, monkeypatch
) -> None:
    """A blocked action's rollback (go_back) keeps the BrowsingInstance, so an unreported window the refused
    page opened keeps a live opener reference across it — unlike the agent's navigate, which swaps the instance
    and cuts the link. The window-birth sweep closes such a window on the rollback, so nothing it planted can
    later run: this is the residual the rejected design left open.

    The window must be open and planted before the rollback, and the rollback must run before the page guard
    blanks the refused page, so the refused page is driven in by hand under the session lock (as the reviewer's
    probe did) and the controller's escape — the escape context manager, the go_back, then the window-birth
    sweep — is run under that same lock, exactly as actions/pipeline.py runs it for a blocked action.

    The assertion is that the window is closed (its born-refused, unreported birth is gone): without the sweep
    that birth stays live. The leak the secret would travel on (the window setting opener.location) does not
    itself reproduce on this Chromium build; closing the window is the defence regardless."""
    install(monkeypatch, plant("'{ALLOWED}/no-content'", target="opener"))
    async with browser_env(tmp_path, {"/start": "<title>allowed</title><p>start</p>"}) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        watcher = env.manager.window_births._get(session.browser)
        done = asyncio.Event()
        session.context.on("console", lambda m: done.set() if m.text == SCRIPT_DONE else None)

        async with session.lock:
            # Renderer-initiated navigation to the refused host, which opens and plants the window; the page
            # guard waits on this lock, so it has not blanked the refused page yet.
            await session.page.evaluate("(url) => { location.href = url }", f"{env.evil}/")
            await asyncio.wait_for(done.wait(), timeout=SCRIPT_TIMEOUT_SECONDS)
            assert env.manager._page_refused(session.page)
            assert _live_born_refused(watcher), "the refused page's window was not born refused and unreported"
            # The blocked action's rollback: go_back to the page read before, then close unreported windows.
            with env.manager.page_provenance.escape(session.page, f"{env.allowed}/start"):
                await session.page.go_back(wait_until="domcontentloaded")
            await env.manager._settle(session.page)
            await env.manager.window_births.close_unreported(session)

        assert session.page.url == f"{env.allowed}/start"
        assert not env.manager._page_refused(session.page)
        await past_the_grace(monkeypatch)
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)

        # The window the refused page opened is closed: no live born-refused unreported window remains.
        assert _live_born_refused(watcher) == [], "an unreported refused window outlived the rollback"
        RELEASED.set()
        await asyncio.sleep(0.8)
        assert LANDING == [], f"the window navigated the agent's tab after the rollback: {LANDING}"
        assert session.page.url == f"{env.allowed}/start", session.page.url
        observed = await env.manager.observe(session_id, preset="text")
        assert SECRET not in json.dumps(observed), observed


# ── legitimate flows are unchanged ─────────────────────────────────────────────


async def test_an_oauth_style_popup_from_a_clean_page_is_not_refused_and_leaves_no_state(tmp_path: Path) -> None:
    """A clean page opens a popup that 302s to an allowed callback, postMessages a code and closes: not refused,
    and the watcher keeps no born-refused entry for it."""
    pages = {
        "/oauth": "<title>allowed</title><p>hi</p>"
        "<script>window.addEventListener('message', (e) => { window.__code = e.data })</script>",
        "/authorize": nav.redirect("{ALLOWED}/callback"),
        "/callback": "<title>cb</title><script>window.opener && window.opener.postMessage('code-123', '*');"
        "setTimeout(() => window.close(), 200)</script>",
    }
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/oauth")
        session = env.manager.sessions[session_id]
        watcher = env.manager.window_births._get(session.browser)

        await session.page.evaluate("(url) => window.open(url)", f"{env.allowed}/authorize")
        # Poll for the code: the popup 302s to the callback, which postMessages it and closes (all async).
        await nav.wait_for(
            lambda: session.page.evaluate("() => window.__code === 'code-123'"), timeout=WINDOW_TIMEOUT_SECONDS
        )
        await settle(lambda: not pending())
        assert all(not b.born_refused for b in watcher._births.values())


async def test_a_target_blank_click_from_a_clean_page_opens_a_readable_tab(tmp_path: Path) -> None:
    pages = {
        "/opener": "<a id=l target=_blank href='{ALLOWED}/second'>link</a>",
        "/second": "<title>second</title><p>two</p>",
    }
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/opener")
        session = env.manager.sessions[session_id]

        async with session.context.expect_page() as opened:
            await env.manager.click(session_id, selector="#l")
        popup = await opened.value
        await popup.wait_for_load_state()
        await settle(lambda: not pending())

        assert not env.manager._page_refused(popup)
        await env.manager.activate_tab(session_id, session.context.pages.index(popup))
        observed = await env.manager.observe(session_id, preset="text")
        assert "two" in json.dumps(observed)


async def test_the_open_tab_tool_opens_a_readable_tab(tmp_path: Path) -> None:
    """The open-tab tool's tab has no opener, so it is never born refused and never closed."""
    async with browser_env(tmp_path, {"/ok": "<title>ok</title><p>fine</p>"}) as env:
        session_id = await env.create()

        result = await env.manager.open_tab(session_id, f"{env.allowed}/ok", activate=True)
        await settle(lambda: not pending())

        assert result["activated"] is True
        assert "fine" in json.dumps(await env.manager.observe(session_id, preset="text"))


# ── round 2: the window acts inside [clear, sweep], nested windows, and the agent's real flows ────────
#
# The reviewers' probes, kept as tests. The planted window acts at the moment the agent's navigate has cleared
# the opener and before the sweep (RELEASED is set from inside the controller's own post-goto settle), or on a
# fixed timer through the controller's real navigate-redirect and click rollback flows. What the agent sees
# (list_tabs, observe, the console) is asserted, not only /landing.


def _release_in_settle(env: nav.Env, monkeypatch) -> None:
    """Set RELEASED the moment the navigate's goto has returned (the taint has cleared), before the sweep."""
    original = env.manager.actions.settle

    async def settle_then(page):
        RELEASED.set()
        await original(page)

    monkeypatch.setattr(env.manager.actions, "settle", settle_then)


async def _nothing_reached_the_agent(env: nav.Env, session_id: str) -> None:
    session = env.manager.sessions[session_id]
    tabs = await env.manager.list_tabs(session_id)
    assert SECRET not in json.dumps(tabs), tabs
    assert SECRET not in json.dumps(session.console_messages), session.console_messages
    assert SECRET not in json.dumps(session.page_errors), session.page_errors
    observed = await env.manager.observe(session_id, preset="text")
    assert SECRET not in json.dumps(observed, default=str), observed
    for index, page in enumerate(list(session.context.pages)):
        if page is session.page or page.is_closed():
            continue
        # Every other tab is refused (not activatable), or shows nothing of the secret.
        try:
            await env.manager.activate_tab(session_id, index)
        except PermissionError:
            continue
        observed = await env.manager.observe(session_id, preset="text")
        assert SECRET not in json.dumps(observed, default=str), (page.url, observed)


# What the window does once /released loads (ACT in act_plant): open a child (born now, after the clear), write
# the secret and a console logger into it, and commit itself to an allowed URL, which reports it and, through it,
# the child; or commit itself to an allowed URL carrying the secret.
_NESTED_CHILD_THEN_COMMIT = """function (S) {
  const c = window.open('');
  c.document.write('<title>LEAKTITLE ' + S + '</title><p>LEAKBODY ' + S + '</p><scr' + 'ipt>setInterval(() => console.log("LEAKLOG " + document.title), 100)</scr' + 'ipt>');
  c.document.close();
  location.href = '{ALLOWED}/ok';
}"""
_SELF_LANDING = """function (S) {
  location.href = '{ALLOWED}/landing?d=' + encodeURIComponent(S);
}"""


def act_plant(open_arg: str, act: str) -> str:
    """The plant, with the function source ``act`` run with the secret once /released loads."""
    body = _PLANT.replace("__OPEN__", open_arg).replace("__EXTRA__", "")
    return body.replace(
        '__TARGET__.href = "{ALLOWED}/landing?d=" + encodeURIComponent(S)',
        "(' + " + json.dumps(act) + " + ')(S)",
    )


@pytest.mark.parametrize("first_nav", sorted(FIRST_NAVIGATIONS), ids=sorted(FIRST_NAVIGATIONS))
async def test_a_window_opened_by_a_born_refused_window_after_the_clear_is_refused(
    tmp_path: Path, monkeypatch, first_nav: str
) -> None:
    """Propagation, every first-navigation variant: the unreported window acts between the clear and the sweep,
    opening a child that is born now, when nothing in the session counts. The child is born refused because its
    opener was; the window, reported once it commits, is held refused until its birth verdict lands. Nothing
    reaches list_tabs, observe or the console."""
    install(monkeypatch, act_plant(FIRST_NAVIGATIONS[first_nav], _NESTED_CHILD_THEN_COMMIT))
    async with browser_env(tmp_path, {"/ok": "<title>ok</title><p>fine</p>"}) as env:
        session_id = await env.create()
        _release_in_settle(env, monkeypatch)

        await drive_refused_window(env, session_id)
        await env.manager.navigate(session_id, f"{env.allowed}/")
        HANG_RELEASE.set()
        await asyncio.sleep(1.5)
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)

        await _nothing_reached_the_agent(env, session_id)


@pytest.mark.parametrize("first_nav", ["205", "data"])
async def test_a_window_that_commits_an_allowed_url_inside_clear_to_sweep_is_held_refused(
    tmp_path: Path, monkeypatch, first_nav: str
) -> None:
    """The async-taint gap: the window commits an allowed URL carrying the secret right after the clear. Its
    opener is clean when Playwright reports it, so it is held refused until its birth verdict (born refused)
    lands: its console messages are dropped, and it is listed by origin only. (Round 1 let this window's
    content and console through here; the hold plus the born-refused birth verdict keep it out.)"""
    install(monkeypatch, act_plant(FIRST_NAVIGATIONS[first_nav], _SELF_LANDING))
    pages = {"/landing": "<title>landing</title><script>console.log('hello ' + location.href)</script>"}
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        _release_in_settle(env, monkeypatch)

        await drive_refused_window(env, session_id)
        await env.manager.navigate(session_id, f"{env.allowed}/")
        await asyncio.sleep(1.5)
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)

        await _nothing_reached_the_agent(env, session_id)
        landed = [p for p in session.context.pages if "/landing" in p.url]
        assert all(env.manager._page_refused(p) for p in landed), [p.url for p in landed]


_FIRES_ON_A_TIMER = """function (S, FIRE) {
  setTimeout(() => {
    const c = window.open('');
    c.document.write('<title>LEAKTITLE ' + S + '</title><p>LEAKBODY ' + S + '</p><scr' + 'ipt>setInterval(() => console.log("LEAKLOG " + document.title), 200)</scr' + 'ipt>');
    c.document.close();
    location.href = '{ALLOWED}/ok';
  }, FIRE);
}"""


def timer_script(fire_ms: int) -> str:
    """The refused page's script: open a javascript: window and plant the nested-child act on a timer from load."""
    return (
        "<script>(() => { const s = document.body.innerText;"
        "const w = window.open('javascript:void(0)');"
        "setTimeout(() => { w.document.write('<scr' + 'ipt>(' + "
        + json.dumps(_FIRES_ON_A_TIMER)
        + " + ')(' + JSON.stringify(s) + ',' + "
        + str(fire_ms)
        + " + ')</scr' + 'ipt>'); w.document.close(); }, 50);"
        "})()</script>"
    )


@pytest.mark.parametrize("fire_ms", [500, 800, 950])
@pytest.mark.parametrize("flow", ["navigate-redirect", "click-link"])
async def test_the_real_rollback_flows_with_the_window_firing_inside_clear_to_sweep(
    tmp_path: Path, monkeypatch, flow: str, fire_ms: int
) -> None:
    """End to end, no test hook in the timing: the agent's own action lands on the refused page (an allowed URL
    that 302s there, or a click on a link to it), the controller rolls it back, and the window the refused page
    opened fires the nested-child act on a fixed timer from that page's load, at offsets across the rollback's
    [clear, sweep]. The action is blocked, and nothing of the refused page reaches the agent."""
    monkeypatch.setattr(nav, "_Handler", _Handler)
    monkeypatch.setattr(nav, "EVIL_PAGE", nav.EVIL_PAGE + timer_script(fire_ms))
    LANDING.clear()
    pages = {
        "/start": "<title>allowed</title><p>start</p><a id=go href='{EVIL}/'>go</a>",
        "/redir": nav.redirect("{EVIL}/"),
        "/ok": "<p>ok</p>",
    }
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]

        with pytest.raises(Exception) as raised:
            if flow == "navigate-redirect":
                await env.manager.navigate(session_id, f"{env.allowed}/redir")
            else:
                await env.manager.click(session_id, selector="#go")
        assert SECRET not in str(raised.value)
        await asyncio.sleep(3.0)
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)

        tabs = await env.manager.list_tabs(session_id)
        assert SECRET not in json.dumps(tabs), tabs
        assert SECRET not in json.dumps(session.console_messages), session.console_messages
        if not env.manager._page_refused(session.page):
            observed = await env.manager.observe(session_id, preset="text")
            assert SECRET not in json.dumps(observed, default=str), observed
        for page in session.context.pages:
            if page is not session.page:
                assert env.manager._page_refused(page) or page.url == "about:blank", page.url


# ── the sweep cannot be outrun ──────────────────────────────────────────────────

_KEEPS_THE_SESSION_COUNTING = (
    "const s = document.body.innerText;"
    "window.open('{ALLOWED}/ok');"  # W1: commits at once, reported while the opener is refused: tainted, counts
    "const w = window.open('{ALLOWED}/no-content');"  # W2: never commits, unreported
    "await new Promise((r) => setTimeout(r, 300));"
    "w.document.write('<script>const S = ' + JSON.stringify(s) + ';"
    " setTimeout(() => { location.href = \"{ALLOWED}/landing?d=\" + encodeURIComponent(S) }, 3000)</scr' + 'ipt>');"
    "w.document.close();"
)


async def test_a_sibling_refused_tab_does_not_keep_an_unreported_window_alive(tmp_path: Path, monkeypatch) -> None:
    """Reviewer's P5e: a tainted popup keeps the session counting after the navigate, but the window's own
    opener is clean, so the window is closed by the navigate's sweep all the same; it never fires."""
    install(monkeypatch, _KEEPS_THE_SESSION_COUNTING)
    pages = {"/ok": "<title>ok</title><p>fine</p>", "/landing": "<script>console.log('hello')</script>"}
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        watcher = env.manager.window_births._get(session.browser)

        await drive_refused_window(env, session_id)
        await env.manager.navigate(session_id, f"{env.allowed}/ok")
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)

        assert env.manager._session_has_refused_page(session), "the tainted popup should keep the session counting"
        assert _live_born_refused(watcher) == [], "the window outlived its opener's refusal"
        await asyncio.sleep(4.0)
        assert LANDING == [], LANDING
        await _nothing_reached_the_agent(env, session_id)


_POLL = """function poll(path, then) {
  const i = new Image();
  i.onload = then;
  i.onerror = () => setTimeout(() => poll(path, then), 50);
  i.src = '{ALLOWED}' + path + '?' + Date.now();
}"""

# WX opens P (a blank of WX's origin), plants P's script, then closes itself, so P is reported with no opener:
# refused (opener_pending) and counting while on its blank, until it leaves for an allowed URL on its own.
_WX = """function (POLL) {
  const p = window.open('');
  p.document.write('<scr' + 'ipt>const poll = eval("(" + ' + JSON.stringify(POLL) + ' + ")"); poll("/released-p", () => { location.href = "{ALLOWED}/ok?p" });</scr' + 'ipt>');
  p.document.close();
  setTimeout(() => window.close(), 200);
}"""

_W1_NESTED_ON_RELEASE = """function (S, POLL) {
  const poll = eval('(' + POLL + ')');
  poll('/released', () => {
    const c = window.open('');
    c.document.write('<title>LEAKTITLE ' + S + '</title><p>LEAKBODY ' + S + '</p>');
    c.document.close();
    location.href = '{ALLOWED}/ok';
  });
}"""

_NO_RESWEEP = (
    "const s = document.body.innerText;"
    "const w1 = window.open('javascript:void(0)');"
    "const wx = window.open('javascript:void(0)');"
    "await new Promise((r) => setTimeout(r, 300));"
    "w1.document.write('<scr' + 'ipt>(' + "
    + json.dumps(_W1_NESTED_ON_RELEASE)
    + " + ')(' + JSON.stringify(s) + ',' + JSON.stringify("
    + json.dumps(_POLL)
    + ") + ')</scr' + 'ipt>'); w1.document.close();"
    "wx.document.write('<scr' + 'ipt>(' + "
    + json.dumps(_WX)
    + " + ')(' + JSON.stringify("
    + json.dumps(_POLL)
    + ") + ')</scr' + 'ipt>'); wx.document.close();"
    "await new Promise((r) => setTimeout(r, 800));"
)
RELEASED_P = threading.Event()


class _ReleasePHandler(_Handler):
    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        if self.path.startswith("/released-p"):
            if not RELEASED_P.is_set():
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.send_header("Content-Length", str(len(GIF)))
            self.end_headers()
            self.wfile.write(GIF)
            return
        super().do_GET()


async def test_a_window_does_not_outlive_a_refusal_that_ended_without_a_sweep(tmp_path: Path, monkeypatch) -> None:
    """Reviewer's no-resweep case: the session still counts at the navigate (P, a reported opener-less page on
    its blank) and stops counting later another way (P leaves its blank for an allowed URL). The windows the
    refused page opened are closed once their opener is clean, not only at a navigate, rollback or close; and
    nothing W1 could do later reaches the agent."""
    install(monkeypatch, _NO_RESWEEP)
    monkeypatch.setattr(nav, "_Handler", _ReleasePHandler)
    RELEASED_P.clear()
    async with browser_env(tmp_path, {"/ok": "<title>ok</title>"}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        watcher = env.manager.window_births._get(session.browser)

        await drive_refused_window(env, session_id)
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)
        assert _live_born_refused(watcher), "the refused page's windows were not born refused"
        await env.manager.navigate(session_id, f"{env.allowed}/")
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)
        assert _live_born_refused(watcher) == [], "an unreported window outlived its opener's refusal"

        RELEASED_P.set()  # P leaves its blank: the session stops counting, with no agent action
        await settle(lambda: not env.manager._session_has_refused_page(session), timeout=WINDOW_TIMEOUT_SECONDS)
        RELEASED.set()  # W1 would act now, long after the clear
        await asyncio.sleep(1.5)
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)
        await _nothing_reached_the_agent(env, session_id)


# ── never close a page Playwright has reported ──────────────────────────────────


async def test_a_reported_refused_popup_made_active_and_navigated_survives(tmp_path: Path, monkeypatch) -> None:
    """Reviewer's P1: a refused page's popup that Playwright reported while the opener was refused. The agent
    closes the opener tab (the popup becomes the active page) and navigates it to an allowed URL, the documented
    way out: the navigate's sweep must not close the agent's own tab."""
    monkeypatch.setattr(nav, "EVIL_PAGE", nav.EVIL_PAGE + "<script>window.open('{ALLOWED}/pop')</script>")
    pages = {"/start": "<p>start</p>", "/pop": "<title>pop</title>", "/ok": "<title>ok</title><p>fine</p>"}
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        reported: list = []
        session.context.on("page", lambda p: reported.append(p))
        async with session.lock:
            await session.page.evaluate("(u) => { location.href = u }", f"{env.evil}/")
            await nav.wait_for(lambda: len(reported) == 1)
        popup = reported[0]
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)
        assert env.manager._page_refused(popup)

        await env.manager.close_tab(session_id, session.context.pages.index(session.page))
        assert session.page is popup
        await past_the_grace(monkeypatch)
        await env.manager.navigate(session_id, f"{env.allowed}/ok")
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)
        await asyncio.sleep(0.5)

        assert not popup.is_closed(), "the sweep closed the agent's own active tab"
        assert session.page is popup and not env.manager._page_refused(popup)
        assert "fine" in json.dumps(await env.manager.observe(session_id, preset="text"))
        await env.manager.navigate(session_id, f"{env.allowed}/start")


# ── no over-refusal of legitimate popups ────────────────────────────────────────


async def test_a_popup_from_an_open_tab_tab_is_judged_by_that_tab_while_another_tab_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """Reviewer's P2: a tab the open-tab tool opened (its targetId cached at open) opens a clean popup while
    the first tab is refused. The popup is judged by its own opener, not the session: readable, activatable.

    note_reported is a no-op here, so the tab's targetId is cached only by the open-tab read under the lock,
    not by the slower report path: that is the read this exercises, and the window is judged by the tab even
    when nothing else cached its id."""
    monkeypatch.setattr(WindowBirths, "note_reported", lambda self, session, page: None)
    pages = {"/start": "<p>start</p>", "/ok": "<title>ok</title>", "/second": "<title>second</title><p>two</p>"}
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        first = session.page
        await env.manager.open_tab(session_id, f"{env.allowed}/ok", activate=False)
        tab = next(p for p in session.context.pages if p is not first)
        assert env.manager.request_gates.known_target_id(tab) is not None

        await first.evaluate("(u) => { location.href = u }", f"{env.evil}/")
        await settle(lambda: first.url == "about:blank", timeout=WINDOW_TIMEOUT_SECONDS)
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)
        assert env.manager._session_has_refused_page(session)

        async with session.context.expect_page() as opened:
            await tab.evaluate("(u) => { window.open(u) }", f"{env.allowed}/second")
        popup = await opened.value
        await popup.wait_for_load_state()
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)

        assert not env.manager._page_refused(popup), popup.url
        await env.manager.activate_tab(session_id, session.context.pages.index(popup))
        assert "two" in json.dumps(await env.manager.observe(session_id, preset="text"))


# ── fail closed ─────────────────────────────────────────────────────────────────


async def test_a_session_is_not_created_when_the_watcher_cannot_attach(tmp_path: Path, monkeypatch) -> None:
    """As the gate's require: a 503, retryable, with the session's context closed. Without it the session ran
    unwatched and the original leak returned."""

    async def boom(self):
        raise RuntimeError("simulated: browser CDP session unavailable")

    monkeypatch.setattr(WindowBirthWatcher, "attach", boom)
    async with browser_env(tmp_path, {}) as env:
        with pytest.raises(WindowBirthWatcherUnavailable) as raised:
            await env.create()

        assert raised.value.status_code == 503 and raised.value.retryable is True
        assert env.manager.sessions == {}
        assert env.closed_contexts == env.contexts, "the failed session's browser context was left open"
        assert env.manager.window_births.status()["state"] == "attach_failed"


async def test_a_session_is_not_created_when_the_contexts_id_cannot_be_read(tmp_path: Path, monkeypatch) -> None:
    async def times_out(context, page):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(WindowBirths, "_read_context_id", staticmethod(times_out))
    async with browser_env(tmp_path, {}) as env:
        with pytest.raises(WindowBirthWatcherUnavailable):
            await env.create()
        assert env.manager.sessions == {}


async def test_a_lost_watcher_refuses_popups_until_a_replacement_re_registers_the_session(
    tmp_path: Path, monkeypatch
) -> None:
    """The watcher's own CDP session dies while the browser lives. Until a new watcher attaches, a clean page's
    popup in that session is refused (fail closed); the next session's attach re-registers the first session,
    whose popups are judged again."""
    pages = {"/ok": "<title>ok</title>", "/second": "<title>second</title><p>two</p>"}
    async with browser_env(tmp_path, pages) as env:
        births = env.manager.window_births
        monkeypatch.setattr(births, "_lost", lambda watcher: None)  # no eager re-attach: the hold is observable
        session_id = await env.create("/ok")
        session = env.manager.sessions[session_id]
        old = births._get(session.browser)

        await old._cdp.detach()
        await settle(lambda: not old.live, timeout=WINDOW_TIMEOUT_SECONDS)
        assert births.holds_refused(session)
        assert births.status()["state"] == "lost"
        async with session.context.expect_page() as opened:
            await session.page.evaluate("(u) => { window.open(u) }", f"{env.allowed}/second")
        popup = await opened.value
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)
        assert env.manager._page_refused(popup), "a popup born under a lost watcher was let through"

        other_id = await env.create("/ok")
        new = births._get(session.browser)
        assert new is not None and new is not old
        assert new._context_id_of(session) is not None, "the replacement did not re-register the first session"
        assert new._context_id_of(env.manager.sessions[other_id]) is not None
        assert not births.holds_refused(session)
        async with session.context.expect_page() as opened:
            await session.page.evaluate("(u) => { window.open(u) }", f"{env.allowed}/second")
        popup = await opened.value
        await popup.wait_for_load_state()
        await settle(lambda: not pending(), timeout=WINDOW_TIMEOUT_SECONDS)
        assert not env.manager._page_refused(popup), popup.url


# ── the hold until the birth verdict is not a refusal ───────────────────────────


async def test_a_clean_popup_is_activatable_the_moment_it_is_reported(tmp_path: Path, monkeypatch) -> None:
    """A reported popup is held refused (opener_pending) until its birth verdict lands, a CDP round trip after
    the report; the agent's next call can come sooner. activate_tab waits for the verdict rather than refuse the
    popup (three tests of test_allowed_hosts_navigation.py activate a popup right after its report). The verdict
    is delayed here so the hold is in force when activate runs, and it must still end on the popup, not refuse
    it."""
    pages = {"/start": "<p>start</p>", "/second": "<title>second</title><p>two</p>"}
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        births = env.manager.window_births
        release = asyncio.Event()
        original_verdict = births.verdict

        async def slow_verdict(session, page):
            await release.wait()
            return await original_verdict(session, page)

        monkeypatch.setattr(births, "verdict", slow_verdict)

        async with session.context.expect_page() as opened:
            await session.page.evaluate("(u) => { window.open(u) }", f"{env.allowed}/second")
        popup = await opened.value
        index = session.context.pages.index(popup)
        await asyncio.sleep(0.1)
        assert env.manager._page_refused(popup), "the reported popup is not held until its verdict"

        activate = asyncio.ensure_future(env.manager.activate_tab(session_id, index))
        await asyncio.sleep(0.3)
        assert not activate.done(), "activate_tab did not wait for the held popup's verdict"

        release.set()
        await activate

        assert session.page is popup
        assert not env.manager._page_refused(popup)
        assert "two" in json.dumps(await env.manager.observe(session_id, preset="text"))
