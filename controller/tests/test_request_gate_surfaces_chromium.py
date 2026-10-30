"""What the agent sees when the pre-request gate stops a navigation (step 3), on real Chromium.

- navigate: `browser_action_blocked`, 403, not retryable, naming only the origin, over the manager, REST and MCP;
- a click whose navigation was stopped: the action succeeds and reports the block on its own page;
- a popup's first navigation: not pinned on the action that opened it, and marked in the tab list;
- a start URL and a new tab's URL: the same refusal as navigate's;
- the audit: `navigation_blocked` with `source: "request"`, origin only.

The refused host must receive nothing in every case.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.action_errors import BROWSER_ACTION_BLOCKED, BrowserActionError
from app.models import McpToolCallRequest
from app.routes.sessions import create_sessions_router
from app.tool_gateway import McpToolGateway
from tests.test_request_gate_chromium import (  # noqa: F401 - the module's shared Chromium
    ALLOWED,
    REFUSED,
    _shared_chromium,
    eventually,
    gated,
    pytestmark,
)

SECRET_PATH = "/secret/path?token=abc"


def _redirect_to_refused(env) -> str:
    return env.server.url(ALLOWED, f"/r/302?to={env.server.url(REFUSED, SECRET_PATH)}")


def _assert_names_the_origin_only(env, text: str) -> None:
    assert env.server.url(REFUSED, "") in text
    assert "secret" not in text and "token" not in text


def _rest(env) -> httpx.AsyncClient:
    import app.main as main_module

    app = FastAPI()
    app.include_router(create_sessions_router(manager=env.manager))
    app.add_exception_handler(BrowserActionError, main_module.handle_browser_action_error)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


# ── navigate ─────────────────────────────────────────────────────────────────


async def test_navigate_through_a_refused_redirect_is_a_block_naming_the_origin(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        session_id = await env.create()

        with pytest.raises(BrowserActionError) as caught:
            await env.manager.navigate(session_id, _redirect_to_refused(env))

        error = caught.value
        assert (error.code, error.status_code, error.retryable) == (BROWSER_ACTION_BLOCKED, 403, False)
        _assert_names_the_origin_only(env, error.message)
        assert env.server.hits_on(REFUSED) == []


async def test_navigate_over_rest(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        session_id = await env.create()

        async with _rest(env) as client:
            response = await client.post(
                f"/sessions/{session_id}/actions/navigate", json={"url": _redirect_to_refused(env)}
            )

        assert response.status_code == 403
        body = response.json()
        assert (body["code"], body["retryable"]) == (BROWSER_ACTION_BLOCKED, False)
        _assert_names_the_origin_only(env, body["error"])
        assert env.server.hits_on(REFUSED) == []


async def test_navigate_over_mcp(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        session_id = await env.create()
        gateway = McpToolGateway(manager=env.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())

        result = await gateway.call_tool(
            McpToolCallRequest(
                name="browser.execute_action",
                arguments={
                    "session_id": session_id,
                    "action": {"action": "navigate", "url": _redirect_to_refused(env), "reason": "test"},
                },
            )
        )

        assert result.isError, result.structuredContent
        assert (result.structuredContent["code"], result.structuredContent["retryable"]) == (
            BROWSER_ACTION_BLOCKED,
            False,
        )
        _assert_names_the_origin_only(env, result.structuredContent["error"])
        assert env.server.hits_on(REFUSED) == []


# ── a click, a popup, the tab list ───────────────────────────────────────────


async def test_a_click_whose_navigation_was_stopped_succeeds_and_reports_the_block(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        env.server.pages["/links"] = f'<a id="go" href="{env.server.url(REFUSED, SECRET_PATH)}">go</a>'
        session_id = await env.create("/links")

        result = await env.manager.click(session_id, selector="#go")

        [block] = result["target"]["blocked_navigations"]
        assert (block["origin"], block["reason"]) == (env.server.url(REFUSED, ""), "off_list")
        _assert_names_the_origin_only(env, str(result["target"]))
        assert env.server.hits_on(REFUSED) == []


async def test_a_popups_block_is_not_pinned_on_the_click_that_opened_it_and_is_listed(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        env.server.pages["/opener"] = (
            f'<button id="go" onclick="window.open(\'{env.server.url(REFUSED, SECRET_PATH)}\')">go</button>'
        )
        session_id = await env.create("/opener")
        session = env.manager.sessions[session_id]

        result = await env.manager.click(session_id, selector="#go")
        await eventually(lambda: env.gate().blocked_count(session.gate_context_id) == 1)
        await eventually(lambda: len(session.context.pages) == 2)
        await eventually(lambda: session.context.pages[1].url.startswith("chrome-error:"))
        tabs = await env.manager.list_tabs(session_id)

        assert "blocked_navigations" not in result["target"], "the popup's block was reported on its opener's click"
        [popup] = [tab for tab in tabs if not tab["active"]]
        assert popup["blocked_navigation"]["origin"] == env.server.url(REFUSED, "")
        _assert_names_the_origin_only(env, str(tabs))
        assert env.server.hits_on(REFUSED) == []


# ── outside the pipeline: a start URL, a new tab ─────────────────────────────


async def test_a_start_url_that_is_stopped_is_the_same_block(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        async with _rest(env) as client:
            response = await client.post("/sessions", json={"name": "s", "start_url": _redirect_to_refused(env)})

        assert response.status_code == 403
        body = response.json()
        assert (body["code"], body["retryable"], body["action"]) == (BROWSER_ACTION_BLOCKED, False, "create_session")
        _assert_names_the_origin_only(env, body["error"])
        assert env.manager.sessions == {}
        assert env.server.hits_on(REFUSED) == []


async def test_a_new_tab_whose_url_is_stopped_is_the_same_block(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        session_id = await env.create()

        with pytest.raises(BrowserActionError) as caught:
            await env.manager.open_tab(session_id, _redirect_to_refused(env), True)

        assert (caught.value.code, caught.value.action) == (BROWSER_ACTION_BLOCKED, "open_tab")
        _assert_names_the_origin_only(env, caught.value.message)
        assert len(env.manager.sessions[session_id].context.pages) == 1, "the failed tab was left open"
        assert env.server.hits_on(REFUSED) == []


# ── the audit ────────────────────────────────────────────────────────────────


async def test_each_block_is_audited_by_origin_with_its_source(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        session_id = await env.create()
        with pytest.raises(BrowserActionError):
            await env.manager.navigate(session_id, _redirect_to_refused(env))

        requested: list = []
        for _ in range(100):  # the audit is written in a task after the gate has answered Chromium
            events = await env.manager.list_audit_events(session_id=session_id, event_type="navigation_blocked")
            requested = [event for event in events if event["details"].get("source") == "request"]
            if requested:
                break
            await asyncio.sleep(0.05)

        assert [event["details"] for event in requested] == [
            {"url": env.server.url(REFUSED, ""), "source": "request", "reason": "off_list"}
        ]


async def test_r6_a_selector_timeout_while_the_page_was_stopped_keeps_its_own_error(tmp_path: Path) -> None:
    """The page sends itself to a refused host while the click waits for an element that never comes."""
    async with gated(tmp_path) as env:
        env.manager.settings.action_timeout_ms = 3000
        # Late enough to land while the click waits, not before the action counts the page's blocks.
        env.server.pages["/wanders"] = (
            f"<script>setTimeout(() => {{ location.href = '{env.server.url(REFUSED, '/away')}'; }}, 1200)</script>"
        )
        session_id = await env.create("/wanders")
        session = env.manager.sessions[session_id]

        with pytest.raises(BrowserActionError) as caught:
            await env.manager.click(session_id, selector="#never-there")

        assert env.gate().blocked_count(session.gate_context_id) == 1, "the control: the page was stopped meanwhile"
        assert caught.value.code != BROWSER_ACTION_BLOCKED, caught.value.message
        assert env.server.hits_on(REFUSED) == []


async def test_r7_a_tab_that_loaded_an_allowed_page_since_its_block_is_not_labelled_by_it(tmp_path: Path) -> None:
    """A refused navigation, an allowed one that succeeds, then an unrelated failure: no stale label."""
    async with gated(tmp_path) as env:
        session_id = await env.create()
        page = env.manager.sessions[session_id].page
        with contextlib.suppress(Exception):
            await page.goto(env.server.url(REFUSED, "/x"))
        await eventually(lambda: page.url.startswith("chrome-error:"))  # a blocked goto raises before it commits
        [tab] = await env.manager.list_tabs(session_id)
        assert tab["blocked_navigation"]["origin"] == env.server.url(REFUSED, ""), "the control: labelled first"
        await page.goto(env.server.url(ALLOWED, "/fine"))

        with contextlib.suppress(Exception):
            await page.goto(f"http://{ALLOWED}:9/unreachable")  # refused by the port, not by the gate
        await eventually(lambda: page.url.startswith("chrome-error:"))
        [tab] = await env.manager.list_tabs(session_id)

        assert "blocked_navigation" not in tab, tab


async def test_f7_a_later_navigation_that_failed_for_another_reason_drops_the_label(tmp_path: Path) -> None:
    """A block, then an allowed navigation that fails (the port refuses) with no commit between: no label."""
    async with gated(tmp_path) as env:
        session_id = await env.create()
        page = env.manager.sessions[session_id].page
        with contextlib.suppress(Exception):
            await page.goto(env.server.url(REFUSED, "/x"))
        await eventually(lambda: page.url.startswith("chrome-error:"))
        [tab] = await env.manager.list_tabs(session_id)
        assert "blocked_navigation" in tab, "the control: labelled first"

        with contextlib.suppress(Exception):
            await page.goto(f"http://{ALLOWED}:9/unreachable")
        await asyncio.sleep(0.5)
        [tab] = await env.manager.list_tabs(session_id)

        assert tab["url"].startswith("chrome-error:")
        assert "blocked_navigation" not in tab, tab


async def test_f5_a_block_from_the_totp_follow_up_is_reported(tmp_path: Path) -> None:
    """The TOTP autofill's submit leads to a refused host: the gate stops it, and the action reports it."""
    async with gated(tmp_path) as env:
        env.server.pages["/form"] = '<button id="noop">noop</button>'
        session_id = await env.create("/form")
        page = env.manager.sessions[session_id].page

        async def auto_submit(_session):
            with contextlib.suppress(Exception):
                await page.evaluate("url => { location.href = url; }", env.server.url(REFUSED, SECRET_PATH))
            await eventually(lambda: page.url.startswith("chrome-error:"))
            return {"filled": True}

        env.manager._maybe_handle_totp = auto_submit

        result = await env.manager.click(session_id, selector="#noop")

        [block] = result["target"]["blocked_navigations"]
        assert block["origin"] == env.server.url(REFUSED, "")
        assert env.server.hits_on(REFUSED) == []


async def test_f9b_a_stopped_navigates_url_is_the_refused_origin(tmp_path: Path) -> None:
    """As for a start URL or a new tab: ``url`` names the refused origin, not the page the action went back to."""
    async with gated(tmp_path) as env:
        session_id = await env.create()

        with pytest.raises(BrowserActionError) as caught:
            await env.manager.navigate(session_id, _redirect_to_refused(env))

        assert caught.value.url == env.server.url(REFUSED, "")
        assert caught.value.payload["url"] == env.server.url(REFUSED, "")


async def test_f9c_a_start_url_that_meta_refreshes_to_a_refused_host_is_a_block(tmp_path: Path) -> None:
    async with gated(tmp_path) as env:
        env.server.pages["/leaves"] = (
            f'<meta http-equiv="refresh" content="0;url={env.server.url(REFUSED, SECRET_PATH)}"><p>leaving</p>'
        )

        with pytest.raises(BrowserActionError) as caught:
            await env.create("/leaves")

        assert (caught.value.code, caught.value.action, caught.value.url) == (
            BROWSER_ACTION_BLOCKED,
            "create_session",
            env.server.url(REFUSED, ""),
        )
        _assert_names_the_origin_only(env, caught.value.message)
        assert env.manager.sessions == {}
        assert env.server.hits_on(REFUSED) == []
