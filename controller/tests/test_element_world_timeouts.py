"""Every step of a world call the caller bounds uses the caller's bound, not the default action timeout (B1).

get_html(text_only) and a selector find_elements pass a longer bound than ACTION_TIMEOUT_MS (the script runs up to
FIND_ELEMENTS_QUERY_TIMEOUT_SECONDS); only the script's own message used it, so a world slow to create failed at the
default first. These run the real PageWorld against a fake page whose CDP session answers slowly, one step at a time.
tests/test_page_reads_isolated_world_chromium.py covers the real thing against Chromium.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.action_errors import PAGE_NOT_RESPONDING, BrowserActionError
from app.browser.element_world import READ, PageWorld

DEFAULT_MS = 40  # ACTION_TIMEOUT_MS here
CALLER_MS = 2000  # what the caller bounds the call at
SLOW_S = 0.25  # past the default, well inside the caller's bound
HUNG_S = 30.0

ANSWERS: dict[str, dict[str, Any]] = {
    "Page.getFrameTree": {"frameTree": {"frame": {"id": "frame-1"}}},
    "Page.createIsolatedWorld": {"executionContextId": 7},
    "Runtime.callFunctionOn": {"result": {"value": "read"}},
}


class SlowCdp:
    """A CDP session whose methods answer after the delay given for them."""

    def __init__(self, delays: dict[str, float]) -> None:
        self.delays = delays
        self.sent: list[str] = []

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent.append(method)
        await asyncio.sleep(self.delays.get(method, 0))
        return ANSWERS[method]

    async def detach(self) -> None:
        return None


def slow_page(cdp: SlowCdp, attach_s: float = 0.0) -> Any:
    async def new_cdp_session(_page: Any) -> SlowCdp:
        await asyncio.sleep(attach_s)
        return cdp

    return SimpleNamespace(context=SimpleNamespace(new_cdp_session=new_cdp_session))


def world() -> PageWorld:
    return PageWorld(SimpleNamespace(action_timeout_ms=DEFAULT_MS))


STEPS = (
    pytest.param("attach", id="the CDP session's attach"),
    pytest.param("Page.getFrameTree", id="the frame tree"),
    pytest.param("Page.createIsolatedWorld", id="the world's creation"),
    pytest.param("Runtime.callFunctionOn", id="the script's own message"),
)


def one_slow_step(step: str, seconds: float) -> tuple[Any, SlowCdp]:
    cdp = SlowCdp({} if step == "attach" else {step: seconds})
    return slow_page(cdp, seconds if step == "attach" else 0.0), cdp


@pytest.mark.parametrize("step", STEPS)
async def test_a_step_slower_than_the_default_but_inside_the_callers_bound_is_waited_for(step: str) -> None:
    page, cdp = one_slow_step(step, SLOW_S)

    value = await world().call(page, "decl", effect=READ, timeout_ms=CALLER_MS)

    assert value == "read"
    assert cdp.sent == ["Page.getFrameTree", "Page.createIsolatedWorld", "Runtime.callFunctionOn"]


@pytest.mark.parametrize("step", STEPS)
async def test_a_step_slower_than_the_callers_bound_fails_at_that_bound_not_the_default(step: str) -> None:
    page, _cdp = one_slow_step(step, HUNG_S)
    caller_ms = 300

    started = time.monotonic()
    with pytest.raises(BrowserActionError) as raised:
        await world().call(page, "decl", effect=READ, timeout_ms=caller_ms)
    elapsed = time.monotonic() - started

    assert raised.value.code == PAGE_NOT_RESPONDING
    assert f"within {caller_ms} ms" in str(raised.value)
    assert caller_ms / 1000 * 0.8 <= elapsed < HUNG_S / 10, elapsed


@pytest.mark.parametrize("step", STEPS)
async def test_a_call_with_no_bound_of_its_own_keeps_the_default(step: str) -> None:
    page, _cdp = one_slow_step(step, SLOW_S)

    with pytest.raises(BrowserActionError) as raised:
        await world().call(page, "decl", effect=READ)

    assert raised.value.code == PAGE_NOT_RESPONDING
    assert f"within {DEFAULT_MS} ms" in str(raised.value)
