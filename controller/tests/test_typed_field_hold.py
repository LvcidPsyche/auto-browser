"""A type's hold on the field its text goes to, by selector (app.browser.services.actions._TypedField): taken in steps
of its own, within one bound, and given back whatever the page or the caller does. And its checks that focus stays
in the field through the clear, asked of the element found once.

The take was one call of Playwright's Locator.evaluate_handle(timeout=). There the timeout bounds only the wait for
the element: the evaluation on it, and the disposal of the element's handle in a finally, wait for the page with no
bound, and the hold is the caller's only once all three are done. A page that did not answer held the type and its
session, and a type cancelled once the hold was made had nothing to give back: the page kept the hold and its
listeners, and Playwright the handle, until the document went. Through Playwright's own Locator, over fakes of what
it finds.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from playwright._impl._locator import Locator
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.action_errors import BrowserActionError
from app.browser.element_scripts import ELEMENT_HAS_FOCUS_SCRIPT, FIELD_HOLD_READ_SCRIPT, FIELD_HOLD_RELEASE_SCRIPT
from app.browser.services import actions
from app.browser.services.actions import _arrives_by, _TypedField

BOUND_MS = 200
PLAIN = {"type": "text", "name": "note", "id": "note", "placeholder": "", "aria-label": "", "autocomplete": ""}


class FoundField:
    """What Playwright's Locator finds for a type by selector: the field's element, and the hold made on it. One step
    (``stalls``) does not answer until the test lets it (``answers``); ``asked`` says it was reached. ``takes`` says
    how many seconds a step takes to answer, by its name. ``focused`` is what the element answers when asked whether
    focus is in it, a test's to change."""

    def __init__(self, stalls: str | None = None, *, takes: dict[str, float] | None = None) -> None:
        self.stalls = stalls
        self.takes = takes or {}
        self.asked = asyncio.Event()
        self.answers = asyncio.Event()
        self.scripts: list[str] = []
        self.focused = True
        self.hold = SimpleNamespace(evaluate=AsyncMock(side_effect=self._run), dispose=AsyncMock())
        self.element = SimpleNamespace(
            evaluate=AsyncMock(side_effect=self._has_focus),
            evaluate_handle=AsyncMock(side_effect=self._hold),
            dispose=AsyncMock(side_effect=self._give_back),
        )
        self.locator = object.__new__(Locator)
        self.locator._frame = SimpleNamespace(_timeout=lambda timeout: timeout)
        self.locator._selector = "#note"
        self.locator.element_handle = AsyncMock(side_effect=self._find)

    async def _step(self, name: str) -> None:
        await asyncio.sleep(self.takes.get(name, 0.0))
        if name == self.stalls:
            self.asked.set()
            await self.answers.wait()

    async def _find(self, **_given: Any) -> Any:
        await self._step("finding-the-element")
        return self.element

    async def _has_focus(self, _script: str) -> bool:
        await self._step("asking-for-focus")
        return self.focused

    async def _hold(self, *_script: Any) -> Any:
        await self._step("making-the-hold")
        return self.hold

    async def _give_back(self) -> None:
        await self._step("giving-the-element-back")

    async def _run(self, script: str) -> dict[str, str]:
        self.scripts.append(script)
        await self._step("releasing-the-hold" if script == FIELD_HOLD_RELEASE_SCRIPT else "reading-the-hold")
        return PLAIN

    def given_back(self) -> tuple[int, int, int]:
        """How often the hold's release script ran, the hold was disposed of, and the element was."""
        return (
            self.scripts.count(FIELD_HOLD_RELEASE_SCRIPT),
            self.hold.dispose.await_count,
            self.element.dispose.await_count,
        )


async def _until(reached: Callable[[], bool]) -> None:
    """Wait for what nothing awaits: a give-back made in the background."""
    for _ in range(500):
        if reached():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("not reached within 5 s")


@pytest.fixture(autouse=True)
def _a_short_bound() -> Any:
    with patch.object(actions, "FIELD_RECHECK_TIMEOUT_MS", BOUND_MS):
        yield


async def test_a_hold_taken_in_time_is_read_and_given_back_once() -> None:
    # The control: the element found, the hold made on it and read; then read once more as it is released, and both
    # handles given back.
    field = FoundField()
    typed = _TypedField(None, field.locator)

    await typed.take()
    taken = typed.not_secret
    await typed.release()

    assert (taken, typed.not_secret) == (True, True)
    assert field.locator.element_handle.await_args.kwargs == {"timeout": BOUND_MS}
    assert field.scripts == [FIELD_HOLD_READ_SCRIPT, FIELD_HOLD_RELEASE_SCRIPT]
    assert field.given_back() == (1, 1, 1)


@pytest.mark.parametrize("stalls", ["finding-the-element", "making-the-hold", "reading-the-hold"])
async def test_a_take_the_page_does_not_answer_is_given_up_at_its_bound(stalls: str) -> None:
    # Codex's read of FX9c4 (1): still pending 1,200 ms into a take bounded at 1,000. The take says secret at its
    # bound, the text is typed all the same, and what the step answers once it was given up on is given back when it
    # arrives: a hold by its release script, which takes its listeners off the node, and then disposed of. A first
    # read that does not answer leaves a hold the type has: given back with the others, as the type ends.
    field = FoundField(stalls)
    typed = _TypedField(None, field.locator)

    began = time.monotonic()
    await asyncio.wait_for(typed.take(), 5)
    took = time.monotonic() - began
    await typed.release()
    released = field.given_back()
    field.answers.set()

    assert typed.not_secret is False
    assert took < 2, took
    if stalls == "finding-the-element":
        assert released == (0, 0, 0)
        await _until(lambda: field.given_back() == (0, 0, 1))
        field.element.evaluate_handle.assert_not_awaited()
    elif stalls == "making-the-hold":
        assert released == (0, 0, 1)
        await _until(lambda: field.given_back() == (1, 1, 1))
        assert field.scripts == [FIELD_HOLD_RELEASE_SCRIPT]
    else:
        assert released == (1, 1, 1)
        assert field.scripts == [FIELD_HOLD_READ_SCRIPT, FIELD_HOLD_RELEASE_SCRIPT]


@pytest.mark.parametrize("second", ["making-the-hold", "reading-the-hold"])
async def test_the_calls_of_a_take_share_one_bound(second: str) -> None:
    # Two calls of 0.6 s under a bound of a second: each within it, and past it together. The take is given up on
    # at the bound, in the second of them: a hold still being made is given back as it arrives, and one whose first
    # read was cut short is the type's, to give back as it ends. Whole seconds, so a slow run decides nothing.
    field = FoundField(takes={"finding-the-element": 0.6, second: 0.6})
    typed = _TypedField(None, field.locator)

    with patch.object(actions, "FIELD_RECHECK_TIMEOUT_MS", 1000):
        await typed.take()
        taken = typed.not_secret
    await typed.release()

    assert (taken, typed.not_secret) == (False, False)
    await _until(lambda: field.given_back() == (1, 1, 1))
    read = [] if second == "making-the-hold" else [FIELD_HOLD_READ_SCRIPT]
    assert field.scripts == [*read, FIELD_HOLD_RELEASE_SCRIPT]


async def _cancelled_at(field: FoundField) -> _TypedField:
    """A type's take and release, as its operation runs them, cancelled where ``field`` stalls."""
    typed = _TypedField(None, field.locator)

    async def operation() -> None:
        try:
            await typed.take()
        finally:
            await typed.release()

    typing = asyncio.create_task(operation())
    await asyncio.wait_for(field.asked.wait(), 5)
    typing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await typing
    return typed


async def test_a_type_cancelled_once_its_hold_was_made_gives_the_hold_back() -> None:
    # Codex's read of FX9c4 (2): the hold was the type's only once Playwright had disposed of the element's handle,
    # so a cancel there left a hold made and nothing to give it back: no release read and no disposal.
    field = FoundField("giving-the-element-back")

    await _cancelled_at(field)

    assert field.given_back() == (1, 1, 1)


@pytest.mark.parametrize("stalls", ["finding-the-element", "making-the-hold"])
async def test_a_handle_that_arrives_once_its_type_was_cancelled_is_given_back(stalls: str) -> None:
    # A cancelled call does not stop the page: the element is found, the hold made and its listeners put on the
    # node, and the handle arrives with nobody waiting for it.
    field = FoundField(stalls)

    await _cancelled_at(field)
    at_the_cancel = field.given_back()
    field.answers.set()

    if stalls == "finding-the-element":
        assert at_the_cancel == (0, 0, 0)
        await _until(lambda: field.given_back() == (0, 0, 1))
    else:
        assert at_the_cancel == (0, 0, 1)
        await _until(lambda: field.given_back() == (1, 1, 1))


@pytest.mark.parametrize("arrives", ["in-time", "late"])
async def test_a_hold_whose_release_does_not_answer_is_still_disposed_of_at_the_bound(arrives: str) -> None:
    # Each step of a give-back is bounded on its own, the one made in the background for a hold that arrived late
    # too: a page that does not answer the release read keeps neither the type nor the handle.
    field = FoundField("releasing-the-hold" if arrives == "in-time" else "making-the-hold")
    typed = _TypedField(None, field.locator)

    await asyncio.wait_for(typed.take(), 5)
    began = time.monotonic()
    await asyncio.wait_for(typed.release(), 5)
    took = time.monotonic() - began
    if arrives == "late":
        field.stalls = "releasing-the-hold"
        field.answers.set()
        field.answers = asyncio.Event()

    assert took < 2, took
    assert typed.not_secret is False
    await _until(lambda: field.given_back() == (1, 1, 1))


async def test_an_element_whose_disposal_does_not_answer_holds_no_type() -> None:
    field = FoundField("giving-the-element-back")
    typed = _TypedField(None, field.locator)

    await asyncio.wait_for(typed.take(), 5)
    began = time.monotonic()
    await asyncio.wait_for(typed.release(), 5)
    took = time.monotonic() - began

    assert took < 2, took
    assert typed.not_secret is True
    assert field.given_back() == (1, 1, 1)


# The checks through the clear: a type by selector that clears its field first.
AFTER_SELECT_ALL = " after select-all (nothing was cleared)"
THROUGH_THE_CLEAR = " through the clear"


async def test_the_checks_through_the_clear_and_the_hold_are_of_the_element_found_once() -> None:
    # FX9c4's staged review (15): by selector nothing asked whether focus stayed in the field through the clear. The
    # element the selector names as the type focused it is found once, asked after each key of the clear, and held:
    # found anew by the locator each time, it could be another element by then.
    field = FoundField()
    typed = _TypedField(None, field.locator)

    await typed.find()
    await typed.require_focus(AFTER_SELECT_ALL)
    await typed.require_focus(THROUGH_THE_CLEAR)
    await typed.take()
    await typed.release()

    assert field.locator.element_handle.await_count == 1
    assert [call.args for call in field.element.evaluate.await_args_list] == [(ELEMENT_HAS_FOCUS_SCRIPT,)] * 2
    assert typed.not_secret is True
    assert field.given_back() == (1, 1, 1)


@pytest.mark.parametrize("during", [AFTER_SELECT_ALL, THROUGH_THE_CLEAR])
async def test_a_field_that_lost_focus_through_the_clear_refuses_the_type(during: str) -> None:
    field = FoundField()
    typed = _TypedField(None, field.locator)
    await typed.find()
    field.focused = False

    with pytest.raises(BrowserActionError) as refused:
        await typed.require_focus(during)
    await typed.release()

    assert refused.value.message == f"Keyboard focus would not stay in the target field{during}, so nothing was typed."
    assert (refused.value.code, refused.value.action, refused.value.retryable) == ("focus_lost", "type", True)
    # No hold was made, and the element found is given back.
    assert field.given_back() == (0, 0, 1)


@pytest.mark.parametrize("stalls", ["finding-the-element", "asking-for-focus"])
async def test_a_check_through_the_clear_that_cannot_be_made_refuses_the_type_at_the_bound(stalls: str) -> None:
    # An element that is not found again, or does not answer, has not said that focus is in it: the type is refused
    # as for one that said no, within the bound, and an element that arrives late is given back as it arrives.
    field = FoundField(stalls)
    typed = _TypedField(None, field.locator)

    began = time.monotonic()
    await asyncio.wait_for(typed.find(), 5)
    with pytest.raises(BrowserActionError) as refused:
        await asyncio.wait_for(typed.require_focus(AFTER_SELECT_ALL), 5)
    took = time.monotonic() - began
    await typed.release()
    field.answers.set()

    assert refused.value.code == "focus_lost"
    assert took < 2, took
    await _until(lambda: field.given_back() == (0, 0, 1))


# A step given up on at its bound is nobody's to hear of once it fails or arrives, and a cancel the caller took is
# the caller's, whenever the step answered.


@pytest.mark.parametrize("stalls", ["finding-the-element", "making-the-hold"])
async def test_a_step_that_fails_just_after_its_bound_is_reported_to_the_loop_by_nobody(stalls: str) -> None:
    # An element_handle(timeout=) whose selector matches nothing answers with its own timeout a moment after the bound
    # of the same length: the type has gone on by then. Python 3.14's asyncio.shield hands such a failure to the
    # loop's exception handler, and asyncio's default one logs it at ERROR with a traceback that names the selector
    # (red there before the step was waited for by asyncio.wait; on 3.12 and 3.11 nothing is reported either way).
    field = FoundField()
    given_up_on = asyncio.Event()
    failed = asyncio.Event()

    async def fails_late(*_given: Any, **_by_name: Any) -> Any:
        # Playwright's own timeout comes a moment after the bound. Here it comes when the test says the take is
        # back, and by no clock: a sleep just past the bound fails the step before the take has given it up where
        # the loop stalls that long (a slow runner), and the test then fails over code that is right.
        await given_up_on.wait()
        failed.set()
        raise PlaywrightTimeoutError(f"Timeout {BOUND_MS}ms exceeded.")

    if stalls == "finding-the-element":
        field.locator.element_handle = AsyncMock(side_effect=fails_late)
    else:
        field.element.evaluate_handle = AsyncMock(side_effect=fails_late)
    typed = _TypedField(None, field.locator)
    loop = asyncio.get_running_loop()
    reported: list[str] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context["message"]))
    try:
        await asyncio.wait_for(typed.take(), 5)
        taken = typed.not_secret
        given_up_on.set()  # the take is back, given up at its bound: now the step fails
        await asyncio.wait_for(failed.wait(), 5)
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)
        await typed.release()

    assert taken is False
    assert reported == []


async def test_a_cancel_the_caller_took_is_never_answered_with_the_handle() -> None:
    # Cancelled k loop iterations after the step answered, for each k until the caller has its answer. Python 3.11's
    # asyncio.wait_for answered a caller cancelled in the iterations just after the step had answered with that answer
    # (red there for k of 1 to 3): the cancel was swallowed, and the caller went on with the handle. A cancel that
    # Task.cancel accepted must end the caller as cancelled, and the handle is given back instead.
    loop = asyncio.get_running_loop()
    for k in range(8):
        gate: asyncio.Future[None] = loop.create_future()
        given_back: list[object] = []
        handle = object()

        async def step() -> object:
            await gate
            return handle

        async def give_back(arrived: object) -> None:
            given_back.append(arrived)

        caller = asyncio.ensure_future(_arrives_by(step(), loop.time() + 5, give_back))
        for _ in range(3):
            await asyncio.sleep(0)
        gate.set_result(None)
        for _ in range(k):
            await asyncio.sleep(0)

        took = caller.cancel()
        if took:
            with pytest.raises(asyncio.CancelledError):
                await caller
            await _until(lambda: given_back == [handle])
        else:
            # The cancel came after the caller had its answer: the handle is its own, and nothing is given back.
            assert await caller is handle
        await asyncio.sleep(0.01)
        assert given_back == ([handle] if took else []), k


async def test_a_step_that_has_not_answered_by_its_bound_is_a_timeout_and_is_given_back_when_it_arrives() -> None:
    # The bound's own error, which every caller of _arrives_by reads as a step that did not arrive; the step goes on,
    # and what it answers once it was given up on is handed to the give-back.
    loop = asyncio.get_running_loop()
    gate: asyncio.Future[None] = loop.create_future()
    given_back: list[object] = []
    handle = object()

    async def step() -> object:
        await gate
        return handle

    async def give_back(arrived: object) -> None:
        given_back.append(arrived)

    with pytest.raises(TimeoutError):
        await _arrives_by(step(), loop.time() + BOUND_MS / 1000, give_back)
    assert given_back == []
    gate.set_result(None)

    await _until(lambda: given_back == [handle])
    for _ in range(20):
        await asyncio.sleep(0)
    assert given_back == [handle]  # once, and not again a turn later


@pytest.mark.parametrize("yields", [0, 1], ids=["answers-at-once", "yields-once"])
async def test_a_step_asked_once_its_bound_had_passed_is_a_timeout_and_is_given_back(yields: int) -> None:
    # A take's steps share one end, so a step that used the whole budget leaves the next one none. Waited for with
    # no time left, a step that answers within a loop turn or two was still found done, and the take went on past
    # its bound with that handle. No time left is the bound's error, and the handle is given back as it arrives.
    loop = asyncio.get_running_loop()
    given_back: list[object] = []
    handle = object()

    async def step() -> object:
        for _ in range(yields):
            await asyncio.sleep(0)
        return handle

    async def give_back(arrived: object) -> None:
        given_back.append(arrived)

    with pytest.raises(TimeoutError):
        await _arrives_by(step(), loop.time() - 1.0, give_back)

    await _until(lambda: given_back == [handle])
    for _ in range(20):
        await asyncio.sleep(0)
    assert given_back == [handle]


async def test_a_step_that_answered_by_its_bound_is_accepted_though_its_caller_hears_of_it_late() -> None:
    # The bound is on the step's answer, not on the moment its caller runs again: a loop that is busy past the bound
    # once the step has answered (here it sleeps, as a slow callback of anything else would make it) takes no handle
    # from a take that had it in time.
    loop = asyncio.get_running_loop()
    given_back: list[object] = []
    handle = object()

    async def step() -> object:
        loop.call_soon(time.sleep, 2 * BOUND_MS / 1000)  # runs once the step has answered, before its caller does
        return handle

    async def give_back(arrived: object) -> None:
        given_back.append(arrived)

    ends = loop.time() + BOUND_MS / 1000
    assert await _arrives_by(step(), ends, give_back) is handle
    assert loop.time() > ends
    for _ in range(20):
        await asyncio.sleep(0)
    assert given_back == []
