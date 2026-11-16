"""A type cancelled in the loop turn the page answers one of its bounded calls is cancelled, on every Python.

On Python 3.11, the controller image's, asyncio.wait_for answered a caller cancelled in the turn its call answered
with that answer: the cancel was lost, and the type went on (the next keystroke typed, a take that read on). On 3.12
and later wait_for raises the cancel, so these tests fail only where the image's Python runs them, as CI's
controller-tests job does.
"""

from __future__ import annotations

import asyncio
import gc
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from app.browser.element_scripts import FIELD_HOLD_READ_SCRIPT, FIELD_HOLD_TAKE_SCRIPT
from app.browser.services import actions
from app.browser.services.actions import BrowserActionService, _TypedField


async def _cancelled_as_it_answers(bounded: Callable[[Awaitable[Any]], Awaitable[Any]], answer: Any) -> None:
    """Ask ``bounded`` of a call the page answers in the turn the caller is cancelled in: the cancel must be raised."""
    answered = asyncio.get_running_loop().create_future()
    asked = asyncio.Event()

    async def call() -> Any:
        asked.set()
        return await answered

    task = asyncio.create_task(bounded(call()))
    await asyncio.wait_for(asked.wait(), 5)
    # Both in this turn: the call's answer, then the cancel of the task waiting for it.
    answered.set_result(answer)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


async def test_a_question_asked_of_a_handle_raises_a_cancel_that_lands_as_it_is_answered() -> None:
    await _cancelled_as_it_answers(actions._answered_soon, True)


async def test_a_keystroke_raises_a_cancel_that_lands_as_it_is_acknowledged() -> None:
    await _cancelled_as_it_answers(lambda call: BrowserActionService._typing_step(call, 5.0), None)


async def test_a_key_of_the_clear_raises_a_cancel_that_lands_as_it_is_acknowledged() -> None:
    async def clear(call: Awaitable[Any]) -> None:
        page = SimpleNamespace(keyboard=SimpleNamespace(press=lambda _key: call))
        await BrowserActionService._clear_key(page, "Delete")

    await _cancelled_as_it_answers(clear, None)


async def test_a_take_raises_a_cancel_that_lands_as_its_read_is_answered() -> None:
    # By selector the take's read was bounded by wait_for, and a take that caught the read's answer for a cancel
    # went on to the text.
    async def take(call: Awaitable[Any]) -> None:
        async def evaluate(script: str) -> Any:
            assert script == FIELD_HOLD_READ_SCRIPT, script
            return await call

        hold = SimpleNamespace(evaluate=evaluate, dispose=lambda: asyncio.sleep(0))

        async def evaluate_handle(script: str) -> Any:
            assert script == FIELD_HOLD_TAKE_SCRIPT, script
            return hold

        element = SimpleNamespace(evaluate_handle=evaluate_handle, dispose=lambda: asyncio.sleep(0))

        async def element_handle(**_given: Any) -> Any:
            return element

        await _TypedField(None, SimpleNamespace(element_handle=element_handle)).take()

    await _cancelled_as_it_answers(take, {"type": "text", "name": "note"})


async def test_a_call_that_fails_as_its_caller_is_cancelled_leaves_nothing_for_asyncio_to_report() -> None:
    # B1-fix1 item 6 (Codex B1-6, Opus F1): _within read the end of a call it gave up on only where the call had not
    # ended, so a call that failed in the turn its caller was cancelled left its failure, the page's text in it, for
    # asyncio to report as "Task exception was never retrieved". Its end is read on every way out now.
    loop = asyncio.get_running_loop()
    heard: list[str] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: heard.append(context["message"]))
    try:
        answered = loop.create_future()
        asked = asyncio.Event()

        async def call() -> Any:
            asked.set()
            return await answered

        task = asyncio.create_task(actions._answered_soon(call()))
        await asyncio.wait_for(asked.wait(), 5)
        # Both in this turn: the call's failure, then the cancel of the task waiting for it.
        answered.set_exception(PlaywrightError("the page failed the read"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        del task
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)

    assert heard == [], heard
