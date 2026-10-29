"""A page's own navigation to a refused URL that commits nothing leaves the page readable (round 4c, F2).

The top-level request to the refused URL marks the page refused until that navigation commits or fails. A
204, or an attachment (a download), commits nothing: Chromium reports the navigation's request as failed,
which clears the mark. The entry reset of the mark, removed in round 4, used to cover a failure that never
came; these pin that the failure does come, in a real Chromium.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import test_allowed_hosts_navigation as nav
from tests._chromium import requires_chromium
from tests.test_allowed_hosts_channels import _AttachmentHandler, _NoContentHandler
from tests.test_allowed_hosts_navigation import (  # noqa: F401 - _shared_chromium is the module's Chromium fixture
    _shared_chromium,
    browser_env,
)
from tests.test_allowed_hosts_popups import settle

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

# How long Chromium may take to report the navigation's request as failed.
FAILURE_EVENT_SECONDS = 10.0


class _AbortingHandler(_NoContentHandler, _AttachmentHandler):
    """The harness's server, plus /no-content (a 204) and /file.csv (an attachment) on either host."""


@pytest.mark.parametrize("path", ["/no-content", "/file.csv"], ids=["204", "attachment"])
async def test_a_pages_own_navigation_to_a_refused_url_that_commits_nothing_leaves_it_readable(
    tmp_path: Path, monkeypatch, path: str
) -> None:
    monkeypatch.setattr(nav, "_Handler", _AbortingHandler)
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        committed: list[str] = []
        # Whether the page was refused as its request to the refused URL started: the listeners run in the
        # order they were added, so the controller's has marked the hop by then.
        refused_at_request: list[bool] = []
        session.page.on("framenavigated", lambda frame: committed.append(frame.url))
        session.page.on(
            "request",
            lambda request: (
                refused_at_request.append(env.manager._page_refused(session.page))
                if request.url.startswith(env.evil)
                else None
            ),
        )

        await session.page.evaluate("(url) => { location.href = url }", f"{env.evil}{path}")

        cleared = await settle(
            lambda: bool(refused_at_request) and not env.manager._page_refused(session.page),
            timeout=FAILURE_EVENT_SECONDS,
        )
        assert refused_at_request == [True], refused_at_request
        assert cleared, "no failure event cleared the refused hop"
        assert not any(env.evil in url for url in committed), committed
        await env.manager.navigate(session_id, f"{env.allowed}/")
        observed = await env.manager.observe(session_id, preset="text")
        assert observed["url"] == f"{env.allowed}/"
