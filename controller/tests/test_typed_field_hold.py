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

from app.action_errors import BrowserActionError
from app.browser.element_scripts import ELEMENT_HAS_FOCUS_SCRIPT, FIELD_HOLD_READ_SCRIPT, FIELD_HOLD_RELEASE_SCRIPT
from app.browser.services import actions
from app.browser.services.actions import _TypedField

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
