"""A world call has two bounds: ``timeout_ms`` for the script's own message, ``setup_timeout_ms`` for those before it.

Giving the caller's ``timeout_ms`` to every message of a call was wrong twice. An element action's includes the wait
that belongs to its script alone (action timeout + wait + margin), and the attach, the frame tree and the world's
creation then waited that long too. A page read's is past ``bounded()``'s limit, so a hung setup message ended in
``bounded()``'s generic error with the session kept, not in ``page_not_responding`` with the session dropped and
detached. These run the real PageWorld, ElementTarget and gateway against a fake page whose CDP session answers slowly,
one step at a time, at scaled timings. tests/test_page_reads_isolated_world_chromium.py covers these reads against
Chromium, not their bounds.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.action_errors import PAGE_NOT_RESPONDING, BrowserActionError
from app.browser.element_ids import ElementIds
from app.browser.element_target import ElementTarget
from app.browser.element_world import READ, PageWorld
from app.tool_inputs import FindElementsInput, GetPageHtmlInput
from tests.test_allowed_hosts_reads import gateway_for, manager_for, read_session

DEFAULT_MS = 80  # ACTION_TIMEOUT_MS here
SHORT_MS = 120  # a caller's bound under SLOW_S, told from the default by the error's text
LONG_MS = 3000  # a caller's bound far past SLOW_S
SLOW_S = 0.3  # past the default and SHORT_MS, well inside LONG_MS
HUNG_S = 30.0
ACTION_WAIT_MS = 2000  # what an element action's script waits on the page by itself
TEST_BOUND_S = 10.0

ATTACH = "attach"
SCRIPT = "Runtime.callFunctionOn"
ELEMENT_ID = "op-" + "a" * 24
ANSWERS: dict[str, dict[str, Any]] = {
    "Page.getFrameTree": {"frameTree": {"frame": {"id": "frame-1"}}},
    "Page.createIsolatedWorld": {"executionContextId": 7},
}
SETUP_STEPS = (
    pytest.param(ATTACH, id="the CDP session's attach"),
    pytest.param("Page.getFrameTree", id="the frame tree"),
    pytest.param("Page.createIsolatedWorld", id="the world's creation"),
)


class SlowCdp:
    """A CDP session whose methods answer after the delay given for them."""

    def __init__(self, delays: dict[str, float], script_value: Any = "read") -> None:
        self.delays = delays
        self.script_value = script_value
        self.sent: list[str] = []
        self.detached = 0

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent.append(method)
        await asyncio.sleep(self.delays.get(method, 0))
        return {"result": {"value": self.script_value}} if method == SCRIPT else ANSWERS[method]

    async def detach(self) -> None:
        self.detached += 1


def slow_page(cdp: SlowCdp, attach_s: float = 0.0) -> Any:
    async def new_cdp_session(_page: Any) -> SlowCdp:
        await asyncio.sleep(attach_s)
        return cdp

    return SimpleNamespace(context=SimpleNamespace(new_cdp_session=new_cdp_session))


def world() -> PageWorld:
    return PageWorld(SimpleNamespace(action_timeout_ms=DEFAULT_MS))


def one_slow_step(step: str, seconds: float, script_value: Any = "read") -> tuple[Any, SlowCdp]:
    cdp = SlowCdp({} if step == ATTACH else {step: seconds}, script_value)
    return slow_page(cdp, seconds if step == ATTACH else 0.0), cdp


async def timed_failure(call: Any) -> tuple[float, BrowserActionError]:
    started = time.monotonic()
    with pytest.raises(BrowserActionError) as raised:
        await asyncio.wait_for(call, timeout=TEST_BOUND_S)
    return time.monotonic() - started, raised.value


def waited_ms(error: BrowserActionError) -> int:
    match = re.search(r"within (\d+) ms", str(error))
    assert match, str(error)
    return int(match.group(1))


async def detached(cdp: SlowCdp) -> None:
    while not cdp.detached:
        await asyncio.sleep(0.01)


# -- each bound belongs to its own messages ------------------------------------


@pytest.mark.parametrize("step", SETUP_STEPS)
async def test_a_setup_step_is_waited_for_up_to_setup_timeout_ms_whatever_timeout_ms_says(step: str) -> None:
    page, cdp = one_slow_step(step, SLOW_S)

    value = await world().call(page, "decl", effect=READ, timeout_ms=SHORT_MS, setup_timeout_ms=LONG_MS)

    assert value == "read"
    assert cdp.sent == ["Page.getFrameTree", "Page.createIsolatedWorld", SCRIPT]


@pytest.mark.parametrize("step", SETUP_STEPS)
async def test_a_setup_step_past_setup_timeout_ms_fails_there_not_at_timeout_ms(step: str) -> None:
    page, _cdp = one_slow_step(step, HUNG_S)

    elapsed, error = await timed_failure(
        world().call(page, "decl", effect=READ, timeout_ms=LONG_MS, setup_timeout_ms=SHORT_MS)
    )

    assert error.code == PAGE_NOT_RESPONDING
    assert SHORT_MS / 2 < waited_ms(error) <= SHORT_MS  # what was left of the setup budget
    assert elapsed < LONG_MS / 1000


async def test_the_scripts_message_is_waited_for_up_to_timeout_ms_whatever_setup_timeout_ms_says() -> None:
    page, _cdp = one_slow_step(SCRIPT, SLOW_S)

    assert await world().call(page, "decl", effect=READ, timeout_ms=LONG_MS, setup_timeout_ms=SHORT_MS) == "read"


async def test_the_scripts_message_past_timeout_ms_fails_there_not_at_setup_timeout_ms() -> None:
    page, _cdp = one_slow_step(SCRIPT, HUNG_S)

    elapsed, error = await timed_failure(
        world().call(page, "decl", effect=READ, timeout_ms=SHORT_MS, setup_timeout_ms=LONG_MS)
    )

    assert error.code == PAGE_NOT_RESPONDING
    assert f"within {SHORT_MS} ms" in str(error)
    assert elapsed < LONG_MS / 1000


@pytest.mark.parametrize("step", SETUP_STEPS)
async def test_with_no_setup_bound_a_setup_step_has_the_worlds_own_timeout_whatever_timeout_ms_says(step: str) -> None:
    page, _cdp = one_slow_step(step, SLOW_S)

    _elapsed, error = await timed_failure(world().call(page, "decl", effect=READ, timeout_ms=LONG_MS))

    assert error.code == PAGE_NOT_RESPONDING
    assert f"within {DEFAULT_MS} ms" in str(error)


async def test_with_no_bound_the_scripts_message_has_the_worlds_own_timeout_whatever_setup_timeout_ms_says() -> None:
    page, _cdp = one_slow_step(SCRIPT, SLOW_S)

    _elapsed, error = await timed_failure(world().call(page, "decl", effect=READ, setup_timeout_ms=LONG_MS))

    assert error.code == PAGE_NOT_RESPONDING
    assert f"within {DEFAULT_MS} ms" in str(error)


# -- an element action's setup does not wait for the script's wait ---------------


@pytest.mark.parametrize("step", SETUP_STEPS)
async def test_an_element_actions_hung_setup_fails_at_the_action_timeout_not_that_plus_its_wait_and_margin(
    step: str,
) -> None:
    page, _cdp = one_slow_step(step, HUNG_S)
    target = ElementTarget(world(), page, ELEMENT_ID)

    elapsed, error = await timed_failure(target.evaluate("(el) => 1", effect=READ, wait_ms=ACTION_WAIT_MS))

    assert error.code == PAGE_NOT_RESPONDING
    assert f"within {DEFAULT_MS} ms" in str(error)
    assert elapsed < ACTION_WAIT_MS / 1000


# -- a setup message past its bound ends the session it was on --------------------


@pytest.mark.parametrize("step", SETUP_STEPS)
async def test_a_setup_step_past_its_bound_fails_with_the_session_dropped_and_detached(step: str) -> None:
    # The step answers late rather than never: a late attach is detached when it lands, as a session that timed out is.
    page, cdp = one_slow_step(step, SLOW_S)
    instance = world()

    _elapsed, error = await timed_failure(
        instance.call(page, "decl", effect=READ, timeout_ms=LONG_MS, setup_timeout_ms=SHORT_MS)
    )

    assert error.code == PAGE_NOT_RESPONDING
    assert (instance._cdp, instance._frame_id) == (None, None)
    await asyncio.wait_for(detached(cdp), timeout=TEST_BOUND_S)
    assert cdp.detached == 1


async def test_the_setup_messages_of_one_call_share_one_deadline() -> None:
    # An attach that takes more than half the budget, then a frame tree that hangs: each message used to get the whole
    # budget, so the call outlived an outer bound a little longer than one budget and was cancelled, not failed.
    budget_ms = 1000
    cdp = SlowCdp({"Page.getFrameTree": HUNG_S})
    page = slow_page(cdp, attach_s=0.6)
    instance = world()

    with pytest.raises(BrowserActionError) as raised:
        await asyncio.wait_for(
            instance.call(page, "decl", effect=READ, timeout_ms=LONG_MS, setup_timeout_ms=budget_ms),
            timeout=1.4 * budget_ms / 1000,
        )

    assert raised.value.code == PAGE_NOT_RESPONDING
    assert (instance._cdp, instance._frame_id) == (None, None)
    await asyncio.wait_for(detached(cdp), timeout=TEST_BOUND_S)
    assert cdp.detached == 1


# -- at the gateway: under bounded(), not at the default action timeout ------------


def text_read(gateway: Any) -> Any:
    return gateway._get_html(GetPageHtmlInput(session_id="s1", text_only=True))


def selector_read(gateway: Any) -> Any:
    return gateway._find_elements(FindElementsInput(session_id="s1", selector="#x"))


READS = (
    pytest.param(text_read, "the text", id="get_html text_only"),
    pytest.param(selector_read, [], id="find_elements by selector"),
)


def gateway_over(
    page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, read_bound_s: float, setup_bound_ms: int
) -> tuple[Any, Any, Any]:
    """The real gateway and ElementIds on a still fake page, with bounded()'s limit and the setup bound patched."""
    monkeypatch.setattr("app.browser.page_read.PAGE_READ_TIMEOUT_SECONDS", read_bound_s)
    monkeypatch.setattr("app.tool_gateway.gateway.PAGE_READ_SETUP_TIMEOUT_MS", setup_bound_ms)
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    session.page.move = None
    session.page.context = page.context
    manager.element_ids = ElementIds(SimpleNamespace(action_timeout_ms=DEFAULT_MS))
    return gateway_for(manager), manager, session


@pytest.mark.parametrize("step", SETUP_STEPS)
@pytest.mark.parametrize(("read", "value"), READS)
async def test_a_page_read_whose_setup_is_slow_but_inside_the_setup_bound_gets_its_read(
    step: str, read: Any, value: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page, _cdp = one_slow_step(step, SLOW_S, {"value": value, "issued": [], "tag": "tag"})
    gateway, _manager, _session = gateway_over(
        page, tmp_path, monkeypatch, read_bound_s=2 * LONG_MS / 1000, setup_bound_ms=LONG_MS
    )

    result = await asyncio.wait_for(read(gateway), timeout=TEST_BOUND_S)

    assert (result["content"] if "content" in result else result["elements"]) == value


@pytest.mark.parametrize("step", SETUP_STEPS)
@pytest.mark.parametrize(("read", "value"), READS)
async def test_a_page_read_whose_setup_hangs_ends_as_page_not_responding_before_boundeds_limit(
    step: str, read: Any, value: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page, _cdp = one_slow_step(step, HUNG_S, {"value": value, "issued": [], "tag": "tag"})
    read_bound_s = LONG_MS / 1000
    gateway, manager, session = gateway_over(
        page, tmp_path, monkeypatch, read_bound_s=read_bound_s, setup_bound_ms=SHORT_MS
    )

    elapsed, error = await timed_failure(read(gateway))

    assert error.code == PAGE_NOT_RESPONDING
    assert SHORT_MS / 2 < waited_ms(error) <= SHORT_MS  # what was left of the setup budget
    assert elapsed < read_bound_s
    assert manager.element_ids._world(session.page)._cdp is None
