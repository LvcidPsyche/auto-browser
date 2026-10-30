"""Step 0.1's cases the other gate modules don't already cover: each sends nothing to the refused host.

Covered elsewhere (tests/test_request_gate_chromium.py): a link, a 302 start URL and redirect chain, window.open,
noopener popups, iframes. Here: a ctrl+click into a new tab, a POST form into a new window, a meta refresh, a
cross-origin 307/308 POST redirect into a refused host, and history navigations back to a refused entry, both one
that committed while the gate let it through and the error entry an earlier block left.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.browser.request_gate import OFF_LIST
from tests.test_request_gate_chromium import (  # noqa: F401 - the module's shared Chromium
    ALLOWED,
    REFUSED,
    _shared_chromium,
    eventually,
    gated,
    pytestmark,
)


async def _one_block(env, session) -> None:
    await eventually(lambda: env.gate().blocked_count(session.gate_context_id) == 1)
    [block] = env.gate().blocks(session.gate_context_id)
    assert (block.origin, block.reason) == (env.server.url(REFUSED, ""), OFF_LIST)
    assert env.server.hits_on(REFUSED) == []


async def test_a_ctrl_click_into_a_new_tab_is_stopped(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        env.server.pages["/links"] = f'<a id="go" href="{env.server.url(REFUSED, "/tab")}">go</a>'
        session_id = await env.create("/links")
        session = env.manager.sessions[session_id]

        await session.page.click("#go", modifiers=["ControlOrMeta"])

        await _one_block(env, session)
        [block] = env.gate().blocks(session.gate_context_id)
        assert block.target_id != await env.manager.request_gates.target_id_of(session.page), "not a new tab"


async def test_a_post_form_into_a_new_window_is_stopped(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        env.server.pages["/form"] = (
            f'<form method="post" target="_blank" action="{env.server.url(REFUSED, "/post")}">'
            '<input name="f" value="the-body"><button id="go">go</button></form>'
        )
        session_id = await env.create("/form")
        session = env.manager.sessions[session_id]

        await session.page.click("#go")

        await _one_block(env, session)


async def test_a_meta_refresh_is_stopped(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        env.server.pages["/refresh"] = (
            f'<meta http-equiv="refresh" content="0; url={env.server.url(REFUSED, "/refreshed")}"><p>refresh</p>'
        )
        session_id = await env.create()
        session = env.manager.sessions[session_id]

        await session.page.goto(env.server.url(ALLOWED, "/refresh"))

        await _one_block(env, session)


@pytest.mark.parametrize("code", [307, 308])
async def test_a_post_redirect_into_a_refused_host_is_stopped_with_its_body_unsent(tmp_path: Path, code: int) -> None:
    async with gated(tmp_path) as env:
        env.server.pages["/form"] = (
            f'<form method="post" action="/r/{code}?to={env.server.url(REFUSED, "/target")}">'
            '<input name="f" value="the-body"><button id="go">go</button></form>'
        )
        session_id = await env.create("/form")
        session = env.manager.sessions[session_id]

        await session.page.click("#go")

        await _one_block(env, session)
        [hop] = [hit for hit in env.server.hits_on(ALLOWED) if hit.path == f"/r/{code}"]
        assert hop.body == b"f=the-body", "the control: the allowed hop got the POST"


async def _history(env, session, middle: str) -> None:
    page = session.page
    await page.goto(env.server.url(ALLOWED, "/p1"))
    try:
        await page.goto(middle)
    except Exception:
        pass  # the refused entry: stopped, leaving Chrome's error entry
    await eventually(lambda: page.url != env.server.url(ALLOWED, "/p1"))
    await page.goto(env.server.url(ALLOWED, "/p2"))


async def test_back_to_the_error_entry_a_block_left_is_stopped_again(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        await _history(env, session, env.server.url(REFUSED, "/x"))
        await _one_block(env, session)

        try:
            await session.page.go_back()
        except Exception:
            pass

        await eventually(lambda: env.gate().blocked_count(session.gate_context_id) == 2)
        assert env.server.hits_on(REFUSED) == []


async def test_back_to_a_refused_entry_that_committed_is_stopped(tmp_path: Path) -> None:
    """The entry committed while its host was on ALLOWED_HOSTS; the host is then taken off."""
    async with gated(tmp_path) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        gate = env.gate()
        allowed = env.manager.settings.allowed_hosts
        env.manager.settings.allowed_hosts = f"{allowed},{REFUSED}"
        try:
            await _history(env, session, env.server.url(REFUSED, "/x"))
        finally:
            env.manager.settings.allowed_hosts = allowed
        assert [hit.path for hit in env.server.hits_on(REFUSED) if hit.path == "/x"] == ["/x"], "the control"
        hits_before = len(env.server.hits_on(REFUSED))

        try:
            await session.page.go_back()
        except Exception:
            pass

        await eventually(lambda: gate.blocked_count(session.gate_context_id) == 1)
        assert len(env.server.hits_on(REFUSED)) == hits_before
        assert not session.page.url.startswith(env.server.url(REFUSED, ""))
