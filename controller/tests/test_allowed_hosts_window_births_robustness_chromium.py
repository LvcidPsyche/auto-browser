"""The window-birth watcher under the conditions the round-2 reviews found it wanting — in real Chromium.

Round 2 judged a window at its CDP target's birth (tests/test_allowed_hosts_window_births_chromium.py). The
reviews of that round found the machinery around it fragile: activate_tab deadlocked the session on a refused
verdict; a replacement watcher knew nothing of the windows born under the lost one; a failed create stayed
registered; a popup that closed before its verdict left state behind; the sweep could close a reported page
whose target id read failed; a popup's popup was over-refused; fail-closed refusals named a host; two reads per
open-tab tab; and nothing proved the hold until the verdict. Each is driven here against the real
BrowserManager, with PRE_REQUEST_BLOCKING off (browser_env). `localhost` is allowed; `127.0.0.1`, the same
server, is not.
"""

from __future__ import annotations

import asyncio
import gc
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.browser.services.tabs import TabUnjudgedError
from app.browser.window_births import BirthVerdict, WindowBirthWatcher
from app.readiness import window_birth_watcher_check
from tests import test_allowed_hosts_navigation as nav
from tests import test_allowed_hosts_window_births_chromium as wbc
from tests._background import pending
from tests.test_allowed_hosts_navigation import (  # noqa: F401 - _shared_chromium is the module's Chromium fixture
    _SHARED,
    SECRET,
    _shared_chromium,
    browser_env,
)
from tests.test_allowed_hosts_popups import settle
from tests.test_allowed_hosts_sessionwide import past_the_grace

pytestmark = wbc.pytestmark

TIMEOUT = wbc.WINDOW_TIMEOUT_SECONDS
PAGES = {
    "/start": "<title>start</title><p>start</p>",
    "/ok": "<title>ok</title><p>fine</p>",
    "/second": "<title>second</title><p>two</p>",
}


def cdp_sessions() -> int:
    """The CDP sessions Playwright holds on the shared browser's connection."""
    connection = _SHARED["browser"]._impl_obj._connection
    return sum(1 for obj in list(connection._objects.values()) if type(obj).__name__ == "CDPSession")


async def page_targets(context_id: str) -> list[dict]:
    """The page targets of a context, from the browser itself (a window Playwright never reported has no Page)."""
    cdp = await _SHARED["browser"].new_browser_cdp_session()
    try:
        infos = (await cdp.send("Target.getTargets"))["targetInfos"]
    finally:
        await cdp.detach()
    return [info for info in infos if info["type"] == "page" and info.get("browserContextId") == context_id]


async def open_popup(session, url: str):
    async with session.context.expect_page() as opened:
        await session.page.evaluate("(u) => { window.open(u) }", url)
    return await opened.value


def failing_read(monkeypatch, env: nav.Env, *, spare) -> None:
    """Every targetId read but ``spare``'s fails, as a read past TARGET_ID_TIMEOUT_SECONDS does."""
    gates = env.manager.request_gates
    real = gates.target_id_of

    async def read(page):
        if page is spare:
            return await real(page)
        await asyncio.sleep(0.05)
        return None

    monkeypatch.setattr(gates, "target_id_of", read)


def lose_watcher_without_replacement(monkeypatch, env: nav.Env) -> None:
    monkeypatch.setattr(env.manager.window_births, "_lost", lambda watcher: None)


# ── 1. activate_tab and a verdict that comes out refused ───────────────────────


async def test_activate_tab_returns_a_refused_verdict_within_a_bound_and_blocks_no_other_call(
    tmp_path: Path, monkeypatch
) -> None:
    """Reviewer's D1-D3: activate_tab waited for the verdict under the session lock, and a refused verdict's
    blank waits for that lock, so the session wedged. The wait runs before the lock: list_tabs answers while
    activate waits, the refusal comes once the verdict does, and the lock is free after it."""
    async with browser_env(tmp_path, PAGES) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        release = asyncio.Event()

        async def refused_once_released(session, page):
            await release.wait()
            return BirthVerdict.REFUSED

        monkeypatch.setattr(env.manager.window_births, "verdict", refused_once_released)
        popup = await open_popup(session, f"{env.allowed}/second")
        index = session.context.pages.index(popup)

        activate = asyncio.ensure_future(env.manager.activate_tab(session_id, index))
        await asyncio.sleep(0.3)
        assert not activate.done(), "activate_tab did not wait for the held popup's verdict"
        tabs = await asyncio.wait_for(env.manager.list_tabs(session_id), timeout=2.0)
        assert len(tabs) == 2, "list_tabs was blocked behind activate_tab's wait"

        release.set()
        with pytest.raises(PermissionError, match="cannot be activated"):
            await asyncio.wait_for(activate, timeout=5.0)
        assert not session.lock.locked()
        assert session.page is not popup
        await settle(lambda: popup.url == "about:blank", timeout=TIMEOUT)
        assert env.manager._page_refused(popup)
        assert "start" in json.dumps(await env.manager.observe(session_id, preset="text"))


async def test_activate_tab_of_a_popup_whose_read_fails_returns_the_unjudged_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    """Reviewer's D1 as written: the popup's targetId read fails (every attempt), activate_tab is called at once.
    It returns within the bound, with the refusal that says the tab could not be judged, and the lock is free."""
    async with browser_env(tmp_path, PAGES) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        failing_read(monkeypatch, env, spare=session.page)
        popup = await open_popup(session, f"{env.allowed}/second")

        activate = asyncio.ensure_future(env.manager.activate_tab(session_id, session.context.pages.index(popup)))
        done, _ = await asyncio.wait({activate}, timeout=8.0)

        assert done, "activate_tab hung on a verdict that comes out refused"
        with pytest.raises(TabUnjudgedError, match="could not be judged"):
            activate.result()
        assert not session.lock.locked()
        await settle(lambda: not pending(), timeout=TIMEOUT)
        assert env.manager.page_provenance.refused_unjudged(popup)


# ── 2. a replacement watcher and the windows born under the lost one ──────────


async def test_a_window_born_while_the_watcher_was_lost_is_closed_by_the_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    """The orchestrator's O3 leak: a refused page's window (javascript:void(0), never reported) born during the
    loss; the agent's navigate clears the opener while no watcher sweeps; the replacement then judged the window
    by its clean opener and it leaked. Now the replacement enters it from the discovery replay as unjudged and
    closes it once nothing counts: nothing it held reaches list_tabs, observe or the console."""
    wbc.install(monkeypatch, wbc.plant(wbc.FIRST_NAVIGATIONS["javascript"]))
    async with browser_env(tmp_path, {"/ok": "<title>ok</title><p>fine</p>"}) as env:
        births = env.manager.window_births
        lose_watcher_without_replacement(monkeypatch, env)
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        old = births._get(session.browser)
        context_id = old._context_id_of(session)
        await old._cdp.detach()
        await settle(lambda: not old.live, timeout=TIMEOUT)
        assert births.holds_refused(session)

        await wbc.drive_refused_window(env, session_id)
        await env.manager.navigate(session_id, f"{env.allowed}/ok")
        assert not env.manager._page_refused(session.page)
        await past_the_grace(monkeypatch)
        assert len(await page_targets(context_id)) == 2, "the refused page's window is not open to be found"

        seen_late: list[dict] = []  # the replacement sweeps at once, so its entry is observed as it is made
        original = WindowBirthWatcher._seen_late

        def spy(self, info):
            seen_late.append(dict(info))
            original(self, info)

        monkeypatch.setattr(WindowBirthWatcher, "_seen_late", spy)
        await env.create()  # the replacement watcher attaches: the first session is registered before discovery
        new = births._get(session.browser)
        assert new is not None and new is not old
        late = [info for info in seen_late if info.get("browserContextId") == context_id and info.get("openerId")]
        assert len(late) == 1, "the replacement did not enter the window from the replay"

        await settle(lambda: len(new._session_births(session)) == 1, timeout=TIMEOUT)
        assert len(await page_targets(context_id)) == 1, "the window outlived the refusal after the replacement"
        wbc.RELEASED.set()
        await asyncio.sleep(1.0)
        await settle(lambda: not pending(), timeout=TIMEOUT)
        tabs = await env.manager.list_tabs(session_id)
        assert wbc.LANDING == []
        assert SECRET not in json.dumps(tabs)
        assert SECRET not in json.dumps(await env.manager.observe(session_id, preset="text"), default=str)
        assert SECRET not in json.dumps(session.console_messages)


async def test_a_reported_clean_popups_window_after_a_replacement_is_not_refused(tmp_path: Path, monkeypatch) -> None:
    """A popup reported (and judged clean) before the loss is a known page to the replacement, from the replay:
    a window it opens afterwards is judged by it, not refused for the replacement, even while another tab counts."""
    async with browser_env(tmp_path, PAGES) as env:
        births = env.manager.window_births
        lose_watcher_without_replacement(monkeypatch, env)
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        popup = await open_popup(session, f"{env.allowed}/ok")
        await popup.wait_for_load_state()
        await settle(lambda: not pending(), timeout=TIMEOUT)
        assert not env.manager._page_refused(popup)
        old = births._get(session.browser)
        await old._cdp.detach()
        await settle(lambda: not old.live, timeout=TIMEOUT)

        other_id = await env.create("/start")  # the replacement
        new = births._get(session.browser)
        assert new is not None and new is not old
        popup_id = env.manager.request_gates.known_target_id(popup)
        assert popup_id is not None and new._births[popup_id].reported and not new._births[popup_id].born_refused
        refused = await open_popup(session, f"{env.evil}/")  # another tab counts
        await settle(lambda: env.manager._page_refused(refused), timeout=TIMEOUT)

        async with session.context.expect_page() as opened:
            await popup.evaluate("(u) => { window.open(u) }", f"{env.allowed}/second")
        child = await opened.value
        await child.wait_for_load_state()
        await settle(lambda: not pending(), timeout=TIMEOUT)

        assert not env.manager._page_refused(child), child.url
        await env.manager.close_session(other_id)


# ── 3. a failed create forgets its registration ───────────────────────────────


async def test_creates_that_fail_after_the_watcher_registered_them_are_forgotten(tmp_path: Path) -> None:
    """The orchestrator's O1b and the reviewers' R1: a start URL that redirects off the list, and a tunnel that
    fails, both after watch(). The registry and the watcher's sessions stay at their baseline."""
    pages = {**PAGES, "/redir": nav.redirect("{EVIL}/")}
    async with browser_env(tmp_path, pages) as env:
        births = env.manager.window_births
        good = await env.create("/ok")
        watcher = births._get(env.manager.sessions[good].browser)
        assert (len(births._registered), watcher.session_count) == (1, 1)

        for _ in range(3):
            with pytest.raises(PermissionError):
                await env.create("/redir")
        env.manager._maybe_provision_session_tunnel = AsyncMock(side_effect=RuntimeError("no tunnel"))
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await env.create("/ok")
        gc.collect()

        assert list(env.manager.sessions) == [good]
        assert (len(births._registered), watcher.session_count) == (1, 1)
        assert len(env.closed_contexts) == len(env.contexts) - 1


# ── 4. no retention ────────────────────────────────────────────────────────────


async def test_popups_that_close_before_their_verdict_leave_no_state(tmp_path: Path) -> None:
    """Reviewer's R7/D6: a popup that called window.close() before its birth verdict landed got its provenance
    state recreated and kept, holding the closed Page (30 of them after a burst; base keeps none). And once a
    report resolves, the registry keeps the verdict, not the task."""
    pages = {**PAGES, "/selfclose": "<title>sc</title><script>window.close()</script>"}
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        provenance = env.manager.page_provenance
        before = len(provenance._states)

        for _ in range(30):
            await session.page.evaluate("(u) => { window.open(u) }", f"{env.allowed}/selfclose")
            await asyncio.sleep(0.1)
        await settle(lambda: len(session.context.pages) == 1, timeout=TIMEOUT)
        await settle(lambda: not pending(), timeout=TIMEOUT)
        gc.collect()

        closed_kept = [state for state in provenance._states.values() if state.page.is_closed()]
        assert closed_kept == [], f"{len(closed_kept)} states kept for closed popups"
        assert len(provenance._states) == before
        reports = env.manager.window_births._reports
        assert sum(1 for report in list(reports.values()) if isinstance(report, asyncio.Task)) == 0


# ── 6. the sweep and a reported page whose id read failed ────────────────────


async def test_a_reported_refused_popup_whose_read_failed_survives_the_sweep_as_the_active_tab(
    tmp_path: Path, monkeypatch
) -> None:
    """Reviewer's D5: P1 with the popup's targetId read failing. The popup is refused (right), its birth is never
    marked reported, and the sweep could not match it: it was closed under the agent as the active tab. Now the
    sweep spares the session's unmatched windows while a reported page's id is unknown."""
    monkeypatch.setattr(nav, "EVIL_PAGE", nav.EVIL_PAGE + "<script>window.open('{ALLOWED}/second')</script>")
    async with browser_env(tmp_path, PAGES) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        failing_read(monkeypatch, env, spare=session.page)
        reported: list = []
        session.context.on("page", lambda p: reported.append(p))
        async with session.lock:
            await session.page.evaluate("(u) => { location.href = u }", f"{env.evil}/")
            await nav.wait_for(lambda: len(reported) == 1, timeout=TIMEOUT)
        popup = reported[0]
        await settle(lambda: not pending(), timeout=TIMEOUT)
        assert env.manager._page_refused(popup)

        await env.manager.close_tab(session_id, session.context.pages.index(session.page))
        assert session.page is popup
        await past_the_grace(monkeypatch)
        await env.manager.navigate(session_id, f"{env.allowed}/ok")
        await asyncio.sleep(1.5)  # the recheck timer, which would close it too
        await settle(lambda: not pending(), timeout=TIMEOUT)

        assert not popup.is_closed(), "the sweep closed the agent's own active tab"
        assert session.page is popup and not env.manager._page_refused(popup)
        assert "fine" in json.dumps(await env.manager.observe(session_id, preset="text"))


# ── 8. a popup's popup before the first popup's read lands ─────────────────────


async def test_the_window_of_a_fresh_clean_popup_is_not_refused_while_another_tab_is(tmp_path: Path) -> None:
    """Reviewer's R5 and D3: a clean popup opens a window from its inline script, before its own targetId read
    lands, while another tab is refused. Base judged the window by its clean opener; round 2 refused it by the
    session. It is judged clean, and activating it the moment it is reported neither refuses it nor hangs."""
    pages = {**PAGES, "/opens": "<title>opens</title><script>window.open('/second')</script>"}
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        first = session.page  # the clean tab the popups open from (activating a child moves session.page)
        refused = await open_popup(session, f"{env.evil}/")
        await settle(lambda: env.manager._page_refused(refused), timeout=TIMEOUT)
        assert env.manager._session_has_refused_page(session)

        for _attempt in range(3):
            reported: list = []
            handler = lambda page: reported.append(page)  # noqa: E731 - Playwright wraps a plain function, not a builtin
            session.context.on("page", handler)
            await first.evaluate("(u) => { window.open(u) }", f"{env.allowed}/opens")
            await nav.wait_for(lambda: len(reported) == 2, timeout=TIMEOUT)
            session.context.remove_listener("page", handler)
            child = reported[1]
            await asyncio.wait_for(env.manager.activate_tab(session_id, session.context.pages.index(child)), 8.0)
            assert session.page is child and not env.manager._page_refused(child)
            assert "two" in json.dumps(await env.manager.observe(session_id, preset="text"))
            for page in reported:
                await page.close()
            await settle(lambda: not pending(), timeout=TIMEOUT)


# ── 9. fail-closed refusals say what happened ──────────────────────────────────


async def test_a_popup_whose_read_fails_is_refused_as_unjudged_and_the_refusal_says_so(
    tmp_path: Path, monkeypatch
) -> None:
    """Reviewer's R2: a clean OAuth-style popup whose targetId read fails. It is refused (fail closed) and blanked,
    but the refusal says the tab could not be judged and names the way out, not a host ALLOWED_HOSTS refuses."""
    pages = {
        **PAGES,
        "/authorize": nav.redirect("{ALLOWED}/callback"),
        "/callback": "<title>cb</title><p>callback-body</p>",
    }
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        failing_read(monkeypatch, env, spare=session.page)
        popup = await open_popup(session, f"{env.allowed}/authorize")
        await settle(lambda: not pending(), timeout=TIMEOUT)

        assert env.manager.page_provenance.refused_unjudged(popup)
        assert popup.url == "about:blank"
        with pytest.raises(TabUnjudgedError) as refused:
            await env.manager.activate_tab(session_id, session.context.pages.index(popup))
        assert "could not be judged" in str(refused.value) and "does not allow" not in str(refused.value)
        assert "new tab" in str(refused.value)

        await env.manager.close_tab(session_id, session.context.pages.index(session.page))
        assert session.page is popup
        with pytest.raises(PermissionError, match="could not be judged") as read:
            await env.manager.observe(session_id, preset="text")
        assert "does not allow" not in str(read.value)
        tab = await env.manager.open_tab(session_id, f"{env.allowed}/callback", activate=True)  # the way out
        assert "callback-body" in json.dumps(await env.manager.observe(session_id, preset="text")), tab


async def test_a_popup_born_under_a_lost_watcher_is_refused_as_unjudged_until_the_agent_navigates_it(
    tmp_path: Path, monkeypatch
) -> None:
    """Reviewer's R3: a popup reported while the watcher is lost. Refused as unjudged, with that message; the
    replacement cannot judge it after the fact, so it stays refused until the agent navigates it."""
    async with browser_env(tmp_path, PAGES) as env:
        births = env.manager.window_births
        lose_watcher_without_replacement(monkeypatch, env)
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        old = births._get(session.browser)
        await old._cdp.detach()
        await settle(lambda: not old.live, timeout=TIMEOUT)
        check = window_birth_watcher_check(births.status())
        assert check.status == "warn" and "lost" in check.message

        popup = await open_popup(session, f"{env.allowed}/second")
        await settle(lambda: not pending(), timeout=TIMEOUT)
        assert env.manager.page_provenance.refused_unjudged(popup)
        with pytest.raises(TabUnjudgedError, match="could not be judged"):
            await env.manager.activate_tab(session_id, session.context.pages.index(popup))

        other_id = await env.create("/ok")  # the replacement
        assert not births.holds_refused(session)
        assert env.manager.page_provenance.refused_unjudged(popup), "a replacement cannot judge a window born before it"
        await env.manager.close_tab(session_id, session.context.pages.index(session.page))
        assert session.page is popup
        await env.manager.navigate(session_id, f"{env.allowed}/ok")
        assert not env.manager._page_refused(popup)
        assert "fine" in json.dumps(await env.manager.observe(session_id, preset="text"))
        await env.manager.close_session(other_id)


# ── 10. one targetId read per page ─────────────────────────────────────────────


async def test_an_open_tab_tab_costs_one_cdp_session(tmp_path: Path) -> None:
    """Reviewer's R8: the open-tab tool and the tab's report both read its targetId, and a read keeps its page
    session, so each tab cost two sessions, one of them orphaned. The measurement is the test."""
    async with browser_env(tmp_path, PAGES) as env:
        session_id = await env.create("/start")
        await settle(lambda: not pending(), timeout=TIMEOUT)
        before = cdp_sessions()

        for _ in range(3):
            await env.manager.open_tab(session_id, f"{env.allowed}/ok", activate=False)
        await settle(lambda: not pending(), timeout=TIMEOUT)

        assert cdp_sessions() - before == 3, f"{cdp_sessions() - before} CDP sessions for 3 open-tab tabs"


# ── 11. the hold until the verdict ─────────────────────────────────────────────


async def test_nothing_a_held_popup_emits_is_kept_and_a_clean_verdict_frees_it(tmp_path: Path, monkeypatch) -> None:
    """The orchestrator's O2: the hold from a popup's report to its birth verdict, made observable by holding
    the targetId read on an event. While held, what the popup logs and throws is dropped, it is not read, and
    its title is not listed; once the verdict (CLEAN) lands, the page is usable, and what it emitted meanwhile
    is still not in the session's output."""
    noisy = (
        f"<title>noisy-title</title><p>noisy-body</p><script>console.log('{SECRET}');"
        f"setTimeout(() => {{ throw new Error('{SECRET}') }}, 0)</script>"
    )
    async with browser_env(tmp_path, {**PAGES, "/noisy": noisy}) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        births = env.manager.window_births
        release = asyncio.Event()
        original = births._resolve_target_id

        async def held(page):
            if page is not session.page:
                await release.wait()
            return await original(page)

        monkeypatch.setattr(births, "_resolve_target_id", held)
        emitted: list = []
        session.context.on("console", lambda message: emitted.append(message.text))

        popup = await open_popup(session, f"{env.allowed}/noisy")
        await nav.wait_for(lambda: SECRET in emitted, timeout=TIMEOUT)
        await popup.wait_for_load_state()
        await asyncio.sleep(0.3)

        assert env.manager._page_refused(popup), "the reported popup is not held until its verdict"
        assert SECRET not in json.dumps(session.console_messages)
        assert SECRET not in json.dumps(session.page_errors)
        tabs = await env.manager.list_tabs(session_id)
        assert "noisy-title" not in json.dumps(tabs)
        session.page = popup
        with pytest.raises(PermissionError):
            await env.manager.observe(session_id, preset="text")
        session.page = session.context.pages[0]

        release.set()
        await env.manager.activate_tab(session_id, session.context.pages.index(popup))
        assert session.page is popup and not env.manager._page_refused(popup)
        assert "noisy-body" in json.dumps(await env.manager.observe(session_id, preset="text"))
        assert SECRET not in json.dumps(session.console_messages)
        assert SECRET not in json.dumps(session.page_errors)


# ── 12. inert when every host is allowed ──────────────────────────────────────


@pytest.mark.parametrize("allowed_hosts", ["*", ""], ids=["star", "empty"])
async def test_with_every_host_allowed_nothing_attaches_and_popups_are_as_on_base(
    tmp_path: Path, allowed_hosts
) -> None:
    """Gate G2: with ALLOWED_HOSTS=* or empty no window is ever born refused, so create_session attaches no
    watcher and opens no browser CDP session, a popup is judged at report by its opener alone (no hold), and
    readiness says off."""
    real_browser = _SHARED["browser"]
    opened: list = []
    original = real_browser.new_browser_cdp_session

    async def counting():
        opened.append(1)
        return await original()

    real_browser.new_browser_cdp_session = counting  # type: ignore[method-assign]
    try:
        async with browser_env(tmp_path, PAGES, allowed_hosts=allowed_hosts) as env:
            session_id = await env.create("/start")
            session = env.manager.sessions[session_id]
            births = env.manager.window_births
            assert not births.watching(session) and births.status()["state"] == "off"
            assert window_birth_watcher_check(births.status()).message.startswith("ALLOWED_HOSTS allows every host")

            popup = await open_popup(session, f"{env.allowed}/second")
            # Judged at report, synchronously: no hold, no verdict task, as on base.
            assert not env.manager._page_refused(popup)
            assert popup not in env.manager.diagnostics._birth_judgements
            evil = await open_popup(session, f"{env.evil}/")
            await evil.wait_for_load_state()
            assert not env.manager._page_refused(evil)  # every host is allowed
            await env.manager.activate_tab(session_id, session.context.pages.index(popup))
            assert "two" in json.dumps(await env.manager.observe(session_id, preset="text"))
            assert opened == [], "a browser CDP session was opened with every host allowed"
    finally:
        real_browser.new_browser_cdp_session = original  # type: ignore[method-assign]


# ── 13. cross-site iframes ─────────────────────────────────────────────────────


FRAMER = (
    "<title>framer</title><script>window.addEventListener('message', (e) => {"
    " window.open('{ALLOWED}/second?d=' + encodeURIComponent(e.data)) })</script>"
)


async def test_a_window_a_cross_site_iframe_on_a_refused_page_opens_is_refused(tmp_path: Path, monkeypatch) -> None:
    """The refused page posts what it read into an allowed-origin iframe (out of process), which opens a window to
    an allowed URL carrying it. Chromium names the page, not the frame, as the window's opener: the window is born
    refused by the refused page and stays refused; nothing reaches list_tabs, observe or the console."""
    evil = (
        nav.EVIL_PAGE + "<iframe src='{ALLOWED}/framer'></iframe>"
        "<script>document.querySelector('iframe').onload = () =>"
        " frames[0].postMessage(document.body.innerText, '*')</script>"
    )
    monkeypatch.setattr(nav, "EVIL_PAGE", evil)
    async with browser_env(tmp_path, {**PAGES, "/framer": FRAMER}) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        first = session.page
        first_id = env.manager.request_gates.known_target_id(first)
        reported: list = []
        session.context.on("page", lambda p: reported.append(p))
        async with session.lock:
            await first.evaluate("(u) => { location.href = u }", f"{env.evil}/")
            await nav.wait_for(lambda: len(reported) == 1, timeout=TIMEOUT)
        window = reported[0]
        await settle(lambda: not pending(), timeout=TIMEOUT)

        watcher = env.manager.window_births._get(session.browser)
        birth = next(b for b in watcher._session_births(session) if b.target_id != first_id)
        assert birth.opener_id == first_id, f"openerId {birth.opener_id} is not the page's target {first_id}"
        assert birth.born_refused and not birth.by_session
        assert env.manager._page_refused(window)
        await settle(lambda: window.url == "about:blank", timeout=TIMEOUT)
        tabs = await env.manager.list_tabs(session_id)
        assert SECRET not in json.dumps(tabs) and SECRET not in json.dumps(session.console_messages)
        with pytest.raises(PermissionError):
            await env.manager.activate_tab(session_id, session.context.pages.index(window))


async def test_a_window_a_cross_site_iframe_on_an_allowed_page_opens_is_judged_by_that_page(
    tmp_path: Path, monkeypatch
) -> None:
    """An allowed page frames the refused host (iframes from other hosts are not filtered, by design), and the
    frame opens a window to an allowed URL while another tab is refused. Chromium names the allowed page as the
    opener, so the window is judged by it: clean, as on base, and as the frame's own content is. Stated here so
    the behaviour is a decision, not an accident."""
    evil = nav.EVIL_PAGE + "<script>if (window.top !== window) window.open('{ALLOWED}/second?from=frame')</script>"
    monkeypatch.setattr(nav, "EVIL_PAGE", evil)
    framing = "<title>framing</title><p>framing</p><iframe src='{EVIL}/'></iframe>"
    async with browser_env(tmp_path, {**PAGES, "/framing": framing}) as env:
        session_id = await env.create("/start")
        session = env.manager.sessions[session_id]
        refused = await open_popup(session, f"{env.evil}/")  # another tab is refused
        await settle(lambda: env.manager._page_refused(refused), timeout=TIMEOUT)
        assert env.manager._session_has_refused_page(session)
        first_id = env.manager.request_gates.known_target_id(session.page)

        reported: list = []
        session.context.on("page", lambda p: reported.append(p))
        await env.manager.navigate(session_id, f"{env.allowed}/framing")
        await nav.wait_for(lambda: len(reported) == 1, timeout=TIMEOUT)
        window = reported[0]
        await window.wait_for_load_state()
        await settle(lambda: not pending(), timeout=TIMEOUT)

        watcher = env.manager.window_births._get(session.browser)
        birth = watcher._births[env.manager.request_gates.known_target_id(window)]
        assert birth.opener_id == first_id, f"openerId {birth.opener_id} is not the page's target {first_id}"
        assert not birth.born_refused
        assert not env.manager._page_refused(window) and window.url.endswith("/second?from=frame")
        await env.manager.activate_tab(session_id, session.context.pages.index(window))
        assert "two" in json.dumps(await env.manager.observe(session_id, preset="text"))
