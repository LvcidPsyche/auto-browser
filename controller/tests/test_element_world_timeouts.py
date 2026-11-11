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
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from app.action_errors import PAGE_NOT_RESPONDING, BrowserActionError
from app.browser.element_ids import ElementIds
from app.browser.element_target import ElementTarget
from app.browser.element_world import CHANGES_PAGE, READ, PageWorld
from app.tool_gateway import drag
from app.tool_inputs import FindElementsInput, GetPageHtmlInput
from tests.test_allowed_hosts_reads import gateway_for, manager_for, read_session

DEFAULT_MS = 80  # ACTION_TIMEOUT_MS here
SHORT_MS = 120  # a caller's bound under SLOW_S, told from the default by the error's text
LONG_MS = 3000  # a caller's bound far past SLOW_S
SLOW_S = 0.3  # past the default and SHORT_MS, well inside LONG_MS
HUNG_S = 30.0
ACTION_WAIT_MS = 2000  # what an element action's script waits on the page by itself
TEST_BOUND_S = 10.0
OPENER_MS = 8000  # the setup budget of a call a test holds open: it is let go long before

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


class HeldCdp(SlowCdp):
    """A session that keeps its answer to one method back until the test lets it go."""

    def __init__(self, held: str) -> None:
        super().__init__({})
        self.held = held
        self.let_go = asyncio.Event()

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method != self.held:
            return await super().send(method, params)
        self.sent.append(method)
        await self.let_go.wait()
        return ANSWERS[method]


def held_page(cdp: SlowCdp, let_go: asyncio.Event) -> Any:
    """A page whose CDP session attaches only once the test lets it."""

    async def new_cdp_session(_page: Any) -> SlowCdp:
        await let_go.wait()
        return cdp

    return SimpleNamespace(context=SimpleNamespace(new_cdp_session=new_cdp_session))


async def until(happened: Callable[[], bool]) -> None:
    """Wait for what a test is about to act on: a fixed sleep was a guess at how long a loaded machine takes."""
    async with asyncio.timeout(TEST_BOUND_S):
        while not happened():
            await asyncio.sleep(0.005)


def one_slow_step(step: str, seconds: float, script_value: Any = "read") -> tuple[Any, SlowCdp]:
    cdp = SlowCdp({} if step == ATTACH else {step: seconds}, script_value)
    return slow_page(cdp, seconds if step == ATTACH else 0.0), cdp


async def timed_failure(call: Any) -> tuple[float, BrowserActionError]:
    started = time.monotonic()
    with pytest.raises(BrowserActionError) as raised:
        await asyncio.wait_for(call, timeout=TEST_BOUND_S)
    return time.monotonic() - started, raised.value


def waited_ms(error: BrowserActionError) -> int:
    """What the message that failed waited (the error's text names the whole budget, not what was left of it)."""
    return int(error.details["waited_ms"])


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
    assert waited_ms(error) <= SHORT_MS  # what was left of the setup budget
    assert f"within {SHORT_MS} ms" in str(error)  # the budget, whatever was left of it
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
    assert waited_ms(error) == DEFAULT_MS  # what it waited: with no budget, the same number the text gives


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


class RetriedCdp(SlowCdp):
    """A first session whose script message answers after ``script_s`` that its session is gone."""

    def __init__(self, script_s: float) -> None:
        super().__init__({SCRIPT: script_s})

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        reply = await super().send(method, params)
        if method == SCRIPT:
            self.script_answered_at = time.monotonic()
            raise PlaywrightError("Target closed")
        return reply


def page_whose_second_attach_hangs(first: SlowCdp, first_attach_s: float) -> Any:
    attaches = 0

    async def new_cdp_session(_page: Any) -> SlowCdp:
        nonlocal attaches
        attaches += 1
        if attaches > 1:
            await asyncio.sleep(HUNG_S)
        await asyncio.sleep(first_attach_s)
        return first

    return SimpleNamespace(context=SimpleNamespace(new_cdp_session=new_cdp_session))


async def test_a_retried_call_shares_the_setup_deadline_of_its_first_try() -> None:
    # The first try spends more than half the budget on its attach and its script answers session-gone; the second
    # try's attach hangs. With a deadline of its own the retry waited a whole budget more, so the call outlived an
    # outer bound a little longer than one budget and was cancelled, not failed.
    budget_ms = 1000
    page = page_whose_second_attach_hangs(RetriedCdp(0.0), first_attach_s=0.6)

    with pytest.raises(BrowserActionError) as raised:
        await asyncio.wait_for(
            world().call(page, "decl", effect=READ, timeout_ms=LONG_MS, setup_timeout_ms=budget_ms),
            timeout=1.4 * budget_ms / 1000,
        )

    assert raised.value.code == PAGE_NOT_RESPONDING
    assert waited_ms(raised.value) <= budget_ms - 500  # the attach of the first try took 600 ms of it


async def test_a_setup_message_that_would_start_after_the_budget_is_spent_fails_at_once() -> None:
    # The first try's script (bounded by timeout_ms, not the setup budget) outlasts the whole budget, then answers
    # session-gone: the retry's attach finds no time left and is not waited for at all, though it would hang.
    budget_ms = 1000
    first = RetriedCdp(1.2)
    page = page_whose_second_attach_hangs(first, first_attach_s=0.0)

    _elapsed, error = await timed_failure(
        world().call(page, "decl", effect=READ, timeout_ms=LONG_MS, setup_timeout_ms=budget_ms)
    )
    failed_after_s = time.monotonic() - first.script_answered_at

    assert error.code == PAGE_NOT_RESPONDING
    assert waited_ms(error) == 0
    # It names the budget that was spent: "within 0 ms" said the page had no time at all.
    assert f"within {budget_ms} ms" in str(error)
    assert "within 0 ms" not in str(error)
    # And it says what happened. Nobody held the session's lock: a wait of no time gave up on it all the same,
    # and the retry was told that "another call was still opening this page's session".
    assert "Another call" not in str(error)
    assert "did not answer" in str(error)
    assert failed_after_s < 0.5  # a retry with a budget of its own would have waited a whole second


async def test_a_call_waits_for_the_session_another_call_is_opening_no_longer_than_its_setup_budget() -> None:
    # The session is opened under a lock, and the wait for the lock was no setup message: a read with a budget of
    # 120 ms waited as long as the call beside it took to attach.
    # The attach is held until the waiting call has given up: timed instead, a stalled machine let the attach
    # finish first, and the waiting call then had nothing to wait for.
    for keeps in (True, False):  # whatever the waiting call asked for: it has no session of its own to drop yet
        cdp, attached = SlowCdp({}), asyncio.Event()
        page, instance = held_page(cdp, attached), world()
        opening = asyncio.ensure_future(instance.call(page, "decl", effect=READ, setup_timeout_ms=OPENER_MS))
        await until(instance._lock.locked)

        _elapsed, error = await timed_failure(
            instance.call(page, "decl", effect=READ, timeout_ms=LONG_MS, setup_timeout_ms=SHORT_MS, keep_session=keeps)
        )

        assert error.code == PAGE_NOT_RESPONDING
        assert f"within {SHORT_MS} ms" in str(error)
        # In its own words: this call sent the page nothing, and was told the page "may have crashed or hung, and
        # nothing more was sent to it. Reload the page", which is what a page that did not answer is told.
        assert "sent the page nothing" in str(error)
        assert "crashed" not in str(error)
        # "Retry it", its message says, and the error said retryable: false beside it.
        assert error.retryable is True
        assert 0 < error.details["waited_ms"] <= SHORT_MS  # what was left of its budget when it began to wait
        assert not opening.done()  # it gave up while the other call was still attaching
        # Only the call that waited gave up: the one that was opening the session gets it, and its answer.
        attached.set()
        assert await asyncio.wait_for(opening, timeout=TEST_BOUND_S) == "read"
        assert (instance._cdp, instance._frame_id, cdp.detached) == (cdp, "frame-1", 0)


async def test_a_call_that_gives_up_the_wait_leaves_the_session_the_other_call_already_holds() -> None:
    # The opener has attached and is asking for the frame tree when the waiting call's budget ends. The waiting call
    # has no session of its own yet, whatever it asked for: dropping "the" session there took the opener's from
    # under it, and the opener then sent its next message to nothing.
    cdp = HeldCdp("Page.getFrameTree")
    page, instance = slow_page(cdp), world()
    opening = asyncio.ensure_future(instance.call(page, "decl", effect=READ, setup_timeout_ms=OPENER_MS))
    await until(lambda: "Page.getFrameTree" in cdp.sent)
    assert instance._lock.locked()
    assert (instance._cdp, instance._frame_id) == (cdp, None)

    _elapsed, error = await timed_failure(instance.call(page, "decl", effect=READ, setup_timeout_ms=SHORT_MS))

    assert error.code == PAGE_NOT_RESPONDING
    assert not opening.done()  # it gave up while the other call was still asking
    assert (instance._cdp, cdp.detached) == (cdp, 0)
    cdp.let_go.set()
    assert await asyncio.wait_for(opening, timeout=TEST_BOUND_S) == "read"
    assert (instance._cdp, instance._frame_id, cdp.detached) == (cdp, "frame-1", 0)


# -- a call past its bound, with another call in flight on the same session --------


class SharedCdp(SlowCdp):
    """A session whose script messages take the delays given, in the order they are sent, and whose detach fails the
    ones still in flight, as a real session's does."""

    def __init__(self, script_delays: list[float]) -> None:
        super().__init__({})
        self._script_delays = list(script_delays)
        self._closed = asyncio.Event()
        self.script_sent = asyncio.Event()

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method != SCRIPT:
            return await super().send(method, params)
        self.sent.append(method)
        self.script_sent.set()
        try:
            await asyncio.wait_for(self._closed.wait(), self._script_delays.pop(0))
        except asyncio.TimeoutError:
            return {"result": {"value": self.script_value}}
        raise PlaywrightError("Target closed")

    async def detach(self) -> None:
        await super().detach()
        self._closed.set()


async def a_change_in_flight_beside(instance: PageWorld, page: Any, cdp: SharedCdp) -> asyncio.Task[Any]:
    """A slow change (a focus, a selection) sent on the world's session and not yet answered."""
    change = asyncio.ensure_future(instance.call(page, "decl", effect=CHANGES_PAGE, timeout_ms=LONG_MS))
    # Its script's message has gone out: a fixed wait here was a guess at how long the setup before it takes.
    await asyncio.wait_for(cdp.script_sent.wait(), timeout=TEST_BOUND_S)
    return change


async def test_a_call_past_its_bound_drops_the_session_under_the_call_beside_it() -> None:
    # What every caller gets unless it asks otherwise: the session may be dead, so it is dropped and detached, and
    # a change in flight on it can no longer be confirmed. The change never answers by itself: answering after
    # SLOW_S, it had 130 ms in which the detach had to reach it first.
    cdp = SharedCdp([HUNG_S, HUNG_S])
    page, instance = slow_page(cdp), world()
    change = await a_change_in_flight_beside(instance, page, cdp)

    _elapsed, error = await timed_failure(instance.call(page, "decl", effect=READ, timeout_ms=SHORT_MS))

    assert error.code == PAGE_NOT_RESPONDING
    assert (instance._cdp, instance._frame_id) == (None, None)
    with pytest.raises(BrowserActionError) as unconfirmed:
        await asyncio.wait_for(change, timeout=TEST_BOUND_S)
    assert unconfirmed.value.code == "change_unconfirmed"
    assert cdp.detached == 1


async def test_a_read_that_keeps_the_session_abandons_only_itself_past_its_bound() -> None:
    # STORE-42's read of a click's target has a 2 s bound, far inside ACTION_TIMEOUT_MS: a page busy that long lost
    # its session at every read or write click, under whatever else was in flight on it.
    cdp = SharedCdp([SLOW_S, HUNG_S])
    page, instance = slow_page(cdp), world()
    change = await a_change_in_flight_beside(instance, page, cdp)

    _elapsed, error = await timed_failure(
        instance.call(page, "decl", effect=READ, timeout_ms=SHORT_MS, keep_session=True)
    )

    assert error.code == PAGE_NOT_RESPONDING
    assert (instance._cdp, instance._frame_id) == (cdp, "frame-1")
    assert await asyncio.wait_for(change, timeout=TEST_BOUND_S) == "read"
    assert cdp.detached == 0


@pytest.mark.parametrize("step", SETUP_STEPS)
async def test_a_read_that_keeps_the_session_fails_at_a_hung_setup_step_with_nothing_detached(step: str) -> None:
    page, cdp = one_slow_step(step, SLOW_S)
    instance = world()

    _elapsed, error = await timed_failure(
        instance.call(page, "decl", effect=READ, timeout_ms=LONG_MS, setup_timeout_ms=SHORT_MS, keep_session=True)
    )

    assert error.code == PAGE_NOT_RESPONDING
    await asyncio.sleep(SLOW_S + 0.1)
    # A session that answered is kept for the next call; an attach that lands late was never the world's, and is
    # detached as any late attach is.
    assert cdp.detached == (1 if step == ATTACH else 0)
    assert instance._cdp is (None if step == ATTACH else cdp)
    cdp.delays.clear()
    assert await instance.call(slow_page(cdp), "decl", effect=READ) == "read"


class _KeyedPage:
    """A page ElementIds can key a world by (it holds its pages weakly, which a SimpleNamespace does not allow)."""

    def __init__(self, cdp: SlowCdp) -> None:
        self.context = slow_page(cdp).context


async def test_element_ids_hands_keep_session_to_the_world() -> None:
    cdp = SharedCdp([HUNG_S])
    ids = ElementIds(SimpleNamespace(action_timeout_ms=DEFAULT_MS))
    page = _KeyedPage(cdp)

    _elapsed, error = await timed_failure(
        ids.evaluate(page, "() => 1", timeout_ms=SHORT_MS, setup_timeout_ms=SHORT_MS, keep_session=True)
    )

    assert error.code == PAGE_NOT_RESPONDING
    # A session that is dropped is forgotten at once and detached in a task of its own, which had not run yet when
    # only the detach was looked at here: the test passed with the flag left behind.
    assert ids._world(page)._cdp is cdp
    await asyncio.sleep(0.05)
    assert cdp.detached == 0


# -- at the gateway: under bounded(), not at the default action timeout ------------


def query_read(gateway: Any) -> Any:
    return gateway._find_elements(FindElementsInput(session_id="s1", query="x"))


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
    assert waited_ms(error) <= SHORT_MS  # what was left of the setup budget
    assert elapsed < read_bound_s
    assert manager.element_ids._world(session.page)._cdp is None


async def test_a_find_elements_query_whose_setup_hangs_ends_as_page_not_responding_not_query_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page, _cdp = one_slow_step("Page.getFrameTree", HUNG_S, {"value": [], "issued": [], "tag": "tag"})
    gateway, manager, session = gateway_over(
        page, tmp_path, monkeypatch, read_bound_s=2 * LONG_MS / 1000, setup_bound_ms=LONG_MS
    )
    # As in production, the world's own timeout is past the query's, so only the setup bound can end it first.
    manager.element_ids = ElementIds(SimpleNamespace(action_timeout_ms=int(HUNG_S * 1000)))
    monkeypatch.setattr("app.tool_gateway.gateway.FIND_ELEMENTS_QUERY_SETUP_TIMEOUT_MS", SHORT_MS)
    monkeypatch.setattr("app.tool_gateway.gateway.FIND_ELEMENTS_QUERY_TIMEOUT_SECONDS", LONG_MS / 1000)

    elapsed, error = await timed_failure(query_read(gateway))

    assert error.code == PAGE_NOT_RESPONDING
    assert elapsed < LONG_MS / 1000
    assert manager.element_ids._world(session.page)._cdp is None


# -- a drag's scroll watch: removed under the drag's cleanup bound -----------------


class WorldPage:
    """A page the world can be keyed on (a SimpleNamespace cannot), with the fake CDP session's context."""

    def __init__(self, cdp: SlowCdp) -> None:
        self.context = slow_page(cdp).context


async def test_a_hung_setup_while_a_drag_watch_is_removed_ends_by_its_own_bound_and_drops_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The removal's only bound was the outer wait_for: a hung setup was cancelled there and the world's session stayed
    # cached for the next call, past the point where setup ends a session on any other path (N7).
    monkeypatch.setattr(drag, "UNWATCH_SETUP_TIMEOUT_MS", SHORT_MS, raising=False)
    monkeypatch.setattr(drag, "UNWATCH_TIMEOUT_SECONDS", LONG_MS / 1000)
    cdp = SlowCdp({"Page.getFrameTree": HUNG_S})
    page = WorldPage(cdp)
    manager = SimpleNamespace(element_ids=ElementIds(SimpleNamespace(action_timeout_ms=int(HUNG_S * 1000))))

    await asyncio.wait_for(drag._stop_watching_quietly(manager, SimpleNamespace(page=page), "nonce"), TEST_BOUND_S)

    world_of_page = manager.element_ids._world(page)
    assert (world_of_page._cdp, world_of_page._frame_id) == (None, None)
    await asyncio.wait_for(detached(cdp), timeout=TEST_BOUND_S)
    assert cdp.detached == 1


async def test_the_setup_budget_of_a_watch_removal_is_named_and_below_its_outer_bound() -> None:
    assert drag.UNWATCH_SETUP_TIMEOUT_MS < drag.UNWATCH_TIMEOUT_SECONDS * 1000
