"""The isolated world's CDP session when the tab crashes, the session goes away, or the frame id changes.

Every call into the world is two CDP messages on a session of the
controller's own. A renderer crash leaves that session open with nothing
answering (Fable's probe: no detach event, every send hangs), so a call with no
timeout hung the request. A session Playwright has disposed refuses every later
send, so a world that kept it was broken for the life of the page.

Same harness as tests/test_element_ids.py, with a Chromium of this module's own.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError

from app.action_errors import BrowserActionError
from tests.test_element_ids import TWO_BUTTONS, _shared_chromium, browser_env, pytestmark  # noqa: F401

# How many action timeouts a call on a crashed tab may take before it counts as hung.
_HUNG_AFTER_TIMEOUTS = 3


async def test_a_crashed_tab_fails_in_bounded_time_and_a_new_tab_works(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": TWO_BUTTONS}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")
        page = env.page(session_id)
        crasher = await page.context.new_cdp_session(page)
        # Page.crash never answers: the renderer it would answer from is gone.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(crasher.send("Page.crash"), 2)
        bound = _HUNG_AFTER_TIMEOUTS * env.manager.settings.action_timeout_ms / 1000

        with pytest.raises((PlaywrightError, BrowserActionError)):
            await asyncio.wait_for(env.manager.observe(session_id, preset="text"), bound)
        with pytest.raises((PlaywrightError, BrowserActionError)):
            await asyncio.wait_for(env.manager.click(session_id, element_id=cancel["element_id"]), bound)
        with pytest.raises(BrowserActionError) as raised:
            await asyncio.wait_for(env.manager.element_ids.evaluate(page, "(arg) => arg", 1), bound)
        assert raised.value.code == "page_not_responding" and "did not answer" in raised.value.message

        await env.manager.open_tab(session_id, f"{env.origin}/", activate=True)
        fresh = await env.element(session_id, "Cancel")
        await env.manager.click(session_id, element_id=fresh["element_id"])
        assert await env.page(session_id).title() == "CANCELLED"


async def test_a_page_that_stops_answering_before_the_action_is_refused_with_a_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fable F7: a world timeout in the before-snapshot left the pipeline raw, a 500 through REST."""
    async with browser_env(tmp_path, {"/": TWO_BUTTONS}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")
        world = env.manager.element_ids._world(env.page(session_id))

        async def never(cdp, method, params=None, timeout_ms=None):
            return await world._within(asyncio.Event().wait(), method, timeout_ms or world.timeout_ms)

        monkeypatch.setattr(world, "_send", never)
        env.manager.settings.action_timeout_ms = 500

        refused = await asyncio.wait_for(_refused(env.manager.click(session_id, element_id=cancel["element_id"])), 10)

        assert refused.code == "page_not_responding" and refused.retryable is False
        assert "did not answer within 500 ms" in refused.message
        assert await env.page(session_id).title() == "checkout"


async def test_an_action_that_ran_before_the_page_stopped_answering_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the observation after the click is lost: an error here would invite a second click."""
    async with browser_env(tmp_path, {"/": TWO_BUTTONS}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")
        page = env.page(session_id)
        world = env.manager.element_ids._world(page)
        send, check = world._send, env.manager._check_bot_challenge
        stopped = False

        async def stops_after_the_action(cdp, method, params=None, timeout_ms=None):
            if not stopped:
                return await send(cdp, method, params, timeout_ms)
            return await world._within(asyncio.Event().wait(), method, timeout_ms or world.timeout_ms)

        async def then_stop(session):
            nonlocal stopped
            stopped = True
            return await check(session)

        monkeypatch.setattr(world, "_send", stops_after_the_action)
        monkeypatch.setattr(env.manager, "_check_bot_challenge", then_stop)
        env.manager.settings.action_timeout_ms = 500

        result = await asyncio.wait_for(env.manager.click(session_id, element_id=cancel["element_id"]), 15)

        assert await page.title() == "CANCELLED"
        assert "did not answer within 500 ms" in result["after"]["observation_error"]


async def _refused(call) -> BrowserActionError:
    with pytest.raises(BrowserActionError) as raised:
        await call
    return raised.value


async def test_a_session_that_went_away_is_replaced_and_the_ids_survive(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": TWO_BUTTONS}) as env:
        session_id = await env.create("/")
        before = [item["element_id"] for item in await env.observe(session_id)]
        world = env.manager.element_ids._world(env.page(session_id))
        await world._cdp.detach()

        after = [item["element_id"] for item in await env.observe(session_id)]
        await env.manager.click(session_id, element_id=after[1])

        assert after == before
        assert await env.page(session_id).title() == "CANCELLED"


async def test_a_main_frame_id_that_changed_is_read_again(tmp_path: Path) -> None:
    """Prerender activation can swap the main frame; the world asks for the frame tree again, once."""
    async with browser_env(tmp_path, {"/": TWO_BUTTONS}) as env:
        session_id = await env.create("/")
        before = [item["element_id"] for item in await env.observe(session_id)]
        world = env.manager.element_ids._world(env.page(session_id))
        world._frame_id = "0" * 32

        assert [item["element_id"] for item in await env.observe(session_id)] == before
