"""The controller's own blank has an origin of its own (round 4e, item 1).

The session-wide refusal (manager._session_has_refused_page) leaves out a tab refused only because another
tab was once the page guard has sent it to about:blank, since no refused page can write into that blank.
That rests on Chromium: page.goto("about:blank") is a navigation the browser starts, and its blank gets an
opaque origin, while a blank a page's own script navigates to has that page's origin, which can be a
refused one.

Same harness as test_allowed_hosts_navigation: a real BrowserManager and real Chromium against a local
server, where `localhost` is allowed and `127.0.0.1` is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._chromium import requires_chromium
from tests.test_allowed_hosts_navigation import (  # noqa: F401 - _shared_chromium is the module's Chromium fixture
    _shared_chromium,
    browser_env,
)
from tests.test_allowed_hosts_popups import settle

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

ORIGIN_SCRIPT = "() => window.origin"


async def test_the_page_guards_blank_has_an_opaque_origin(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        page = session.page
        async with session.lock:  # as an action in flight holds it
            await page.evaluate("(url) => { location.href = url }", f"{env.evil}/")
            await page.wait_for_url(f"{env.evil}/")
        assert await settle(lambda: page.url == "about:blank")

        assert await page.evaluate(ORIGIN_SCRIPT) == "null"


async def test_control_a_blank_a_page_navigates_to_itself_has_that_pages_origin(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        page = session.page
        async with session.lock:
            await page.evaluate("() => { location.href = 'about:blank' }")
            await page.wait_for_url("about:blank")

        assert await page.evaluate(ORIGIN_SCRIPT) == env.allowed
