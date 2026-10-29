"""ALLOWED_HOSTS holds for a popup's first page, not only for where the popup goes next.

Popups get the page guard from the context "page" event, and Playwright emits that
event only after the popup's first navigation has committed. The guard's
framenavigated listener was added after the event it needed to see, so a popup a
page opened straight onto a disallowed host stayed there, unaudited, and its
console text and page errors reached every observation. Shadow-browse sessions
had no "page" listener at all.

Same harness as test_allowed_hosts_navigation: a real BrowserManager and real
Chromium against a local server, where `localhost` is allowed and `127.0.0.1`
(the same server) is not.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests import test_allowed_hosts_navigation as nav
from tests._background import pending
from tests._chromium import requires_chromium
from tests.test_allowed_hosts_navigation import (  # noqa: F401 - _shared_chromium is the module's Chromium fixture
    SECRET,
    _shared_chromium,
    _TrackedBrowser,
    audit_spy,
    blocked_events,
    browser_env,
    navigate_later,
    open_popup,
    wait_for,
)

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

OPENER_PAGE = (
    "<title>allowed</title>"
    "<button id=open-evil onclick=\"window.open('{EVIL}/')\">open evil</button>"
    "<a id=blank-evil href='{EVIL}/' target=_blank>evil link</a>"
    "<button id=open-allowed onclick=\"window.open('{ALLOWED}/timer')\">open allowed</button>"
)
SELF_OPENER_PAGE = "<title>allowed</title><script>setTimeout(() => window.open('{EVIL}/'), 300)</script>"
GUARD_GRACE_SECONDS = 3.0


async def settle(condition, timeout: float = GUARD_GRACE_SECONDS) -> bool:
    """Give the guard's background task time to run; report whether `condition` came to hold."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition() and not pending():
            return True
        await asyncio.sleep(0.05)
    return condition()


async def click_and_catch_popup(env: nav.Env, session_id: str, selector: str):
    """Click through the manager, as the REST click route does, and return the popup the click opened."""
    async with env.manager.sessions[session_id].context.expect_page() as opened:
        await env.manager.click(session_id, selector=selector)
    popup = await opened.value
    await popup.wait_for_load_state()
    return popup


def assert_blanked_once(popup, spy: AsyncMock, env: nav.Env, session_id: str) -> None:
    events = blocked_events(spy)
    assert (popup.url, len(events)) == ("about:blank", 1), f"popup on {popup.url}, {len(events)} navigation_blocked"
    assert events[0].kwargs["session_id"] == session_id
    assert events[0].kwargs["details"]["url"].startswith(env.evil)
    assert events[0].kwargs["details"]["source"] == "page"


@pytest.mark.parametrize("opener", ["#open-evil", "#blank-evil"], ids=["window_open", "target_blank_link"])
async def test_a_popup_opened_straight_onto_a_disallowed_host_is_returned_to_blank(tmp_path: Path, opener: str) -> None:
    async with browser_env(tmp_path, {"/opener": OPENER_PAGE}) as env:
        session_id = await env.create("/opener")
        spy = audit_spy(env)

        popup = await click_and_catch_popup(env, session_id, opener)
        await settle(lambda: popup.url == "about:blank")

        assert_blanked_once(popup, spy, env, session_id)


async def test_a_popup_a_page_opens_by_itself_onto_a_disallowed_host_is_returned_to_blank(tmp_path: Path) -> None:
    """No click: the page's own timer opens it, and Chromium lets it."""
    async with browser_env(tmp_path, {"/auto": SELF_OPENER_PAGE}) as env:
        spy = audit_spy(env)
        session_id = await env.create("/auto")
        session = env.manager.sessions[session_id]

        await wait_for(lambda: len(session.context.pages) == 2, timeout=5)
        popup = session.context.pages[1]
        await settle(lambda: popup.url == "about:blank")

        assert_blanked_once(popup, spy, env, session_id)


async def test_control_a_popup_that_starts_allowed_is_still_returned_to_blank_when_it_moves_off(
    tmp_path: Path,
) -> None:
    async with browser_env(tmp_path, {"/opener": OPENER_PAGE, "/timer": nav.TIMER_PAGE}) as env:
        session_id = await env.create("/opener")
        spy = audit_spy(env)

        popup = await click_and_catch_popup(env, session_id, "#open-allowed")
        assert blocked_events(spy) == []
        await navigate_later(popup, f"{env.evil}/")

        assert await settle(lambda: popup.url == "about:blank"), popup.url
        assert_blanked_once(popup, spy, env, session_id)


LOGGING_EVIL_PAGE = nav.EVIL_PAGE + (
    "<script>"
    "const leak = () => document.body.innerText;"
    "console.log('LEAK ' + leak());"
    "setInterval(() => console.log('LEAK ' + leak()), 100);"
    "setInterval(() => { throw new Error('LEAK ' + leak()); }, 100);"
    "</script>"
)


@pytest.mark.parametrize("channel", ["console_messages", "page_errors"])
async def test_output_of_a_disallowed_popup_does_not_reach_the_observation(
    tmp_path: Path, monkeypatch, channel: str
) -> None:
    """The popup runs until the guard gets the session lock, which an action in flight holds."""
    monkeypatch.setattr(nav, "EVIL_PAGE", LOGGING_EVIL_PAGE)
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]

        emitted: list[str] = []
        async with session.lock:
            popup = await open_popup(env, session_id, f"{env.evil}/")
            # The page's own output, as Playwright reports it: the absence below means something.
            popup.on("console", lambda message: emitted.append(message.text))
            popup.on("pageerror", lambda error: emitted.append(str(error)))
            await nav.wait_for(lambda: sum(SECRET in text for text in emitted) >= 3, timeout=10)
        await settle(lambda: popup.url == "about:blank")
        observed = await env.manager.observe(session_id, preset="text")

        assert session.page.url == f"{env.allowed}/", "precondition: the active page is the allowed one"
        leaked = [entry for entry in observed[channel] if SECRET in str(entry)]
        assert leaked == [], f"disallowed page text in the observation ({len(leaked)} entries): {leaked[:1]}"


async def test_control_an_allowed_pages_console_messages_and_page_errors_are_kept(tmp_path: Path) -> None:
    noisy_page = (
        "<title>allowed</title><script>console.log('KEPT-LOG')</script><script>throw new Error('KEPT-ERROR')</script>"
    )
    async with browser_env(tmp_path, {"/noisy": noisy_page}) as env:
        session_id = await env.create("/noisy")
        session = env.manager.sessions[session_id]

        await settle(lambda: bool(session.console_messages and session.page_errors))
        observed = await env.manager.observe(session_id, preset="text")

        assert "KEPT-LOG" in str(observed["console_messages"]), observed["console_messages"]
        assert "KEPT-ERROR" in str(observed["page_errors"]), observed["page_errors"]


async def test_a_popup_in_a_shadow_browse_session_is_returned_to_blank(tmp_path: Path) -> None:
    """The real enable_shadow_browse path, with its headed launch handed this module's headless Chromium.

    The page opens the popup itself and nothing calls the controller afterwards: an
    action's observation would list the tabs, and listing them attaches the listeners.
    """
    async with browser_env(tmp_path, {}) as env:
        source_id = await env.create()
        env.manager.settings.shadow_browse_enabled = True
        launch = AsyncMock(return_value=_TrackedBrowser(nav._SHARED["browser"], env))
        env.manager.playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        shadow_id = (await env.manager.enable_shadow_browse(source_id))["shadow_session_id"]
        assert launch.await_args.kwargs["headless"] is False
        spy = audit_spy(env)

        popup = await open_popup(env, shadow_id, f"{env.evil}/")
        await settle(lambda: popup.url == "about:blank")

        assert_blanked_once(popup, spy, env, shadow_id)
