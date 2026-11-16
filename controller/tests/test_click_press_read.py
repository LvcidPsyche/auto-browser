"""The read made just before a press: what is under the pointer, not what the action named (STORE-42).

The click classifier read the element a click names, before the approval decision. The press lands on what is under
the pointer when the mouse goes down: the "Delete account" button that fills a wrapper the click named, or a row's
"Delete" that the page shows only while the pointer is over the row. Neither had been read, and both ran as
labelled. A press made for an action whose target is read is now read once more, at the pointer, and refused
(PressNeedsApproval) when it is a button of a kind the action has no approval for; execute_decision then asks for
that approval. These pin the read and the three places a press is made; test_click_target_chromium.py runs them
against real Chromium.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.action_errors import PAGE_NOT_RESPONDING, BrowserActionError
from app.browser import click_target
from app.browser.click_target import (
    CLICK_TARGET_SCRIPT,
    PRESS_READ_TIMEOUT_MS,
    PressNeedsApproval,
    press_is_read,
    reading_presses,
    refuse_press_not_asked_about,
)
from app.browser.element_scripts import ELEMENT_HAS_FOCUS_SCRIPT
from app.browser.services import actions
from app.browser.services.actions import (
    _PRESS_PLACE_SCRIPT,
    _RECEIVES_POINTER_SCRIPT,
    ENABLED_POLL_SECONDS,
    PLACE_RECOUNTS,
    POINTER_CHECK_FRAMES,
    POINTER_CHECK_TIMEOUT_MS,
    BrowserActionService,
    _Budget,
)

DELETE = {"tag": "button", "type": "submit", "role": "", "submits": False, "labels": ["", "Delete account"]}
PAY = {"tag": "div", "type": "", "role": "button", "submits": False, "labels": ["Pay now"]}
CLOSE = {"tag": "button", "type": "button", "role": "", "submits": False, "labels": ["Close"]}
PAGE = object()


def _ids(answer: Any) -> SimpleNamespace:
    evaluate = AsyncMock(side_effect=answer) if isinstance(answer, BaseException) else AsyncMock(return_value=answer)
    return SimpleNamespace(evaluate=evaluate)


async def _read(ids: SimpleNamespace) -> None:
    await refuse_press_not_asked_about(ids, PAGE, 40.0, 50.0, session_id="session-7")


async def test_a_press_made_for_no_read_action_asks_the_page_nothing() -> None:
    ids = _ids(DELETE)

    assert press_is_read() is False
    await _read(ids)

    ids.evaluate.assert_not_awaited()


async def test_a_button_of_the_table_under_the_pointer_stops_the_press() -> None:
    ids = _ids(DELETE)

    with reading_presses("click", "read", None):
        assert press_is_read() is True
        with pytest.raises(PressNeedsApproval) as stopped:
            await _read(ids)
    assert press_is_read() is False

    assert (stopped.value.kind, stopped.value.code, stopped.value.retryable) == (
        "destructive",
        "press_needs_approval",
        False,
    )
    assert "Nothing was pressed" in stopped.value.message and "Delete account" not in stopped.value.message
    # Read where the mouse is about to go down, in the isolated world, bounded, the page's session kept.
    page, script, arg = ids.evaluate.await_args.args
    assert (page, script) == (PAGE, CLICK_TARGET_SCRIPT)
    assert (arg["x"], arg["y"]) == (40.0, 50.0) and "elementId" not in arg
    assert ids.evaluate.await_args.kwargs == {
        "timeout_ms": PRESS_READ_TIMEOUT_MS,
        "setup_timeout_ms": PRESS_READ_TIMEOUT_MS,
        "keep_session": True,
    }


async def test_the_kind_the_action_was_approved_for_is_pressed_and_another_kind_is_not() -> None:
    with reading_presses("click", "write", "destructive"):
        await _read(_ids(DELETE))
        with pytest.raises(PressNeedsApproval) as stopped:
            await _read(_ids(PAY))

    assert stopped.value.kind == "payment"


@pytest.mark.parametrize("label", ["read", "write", "upload"])
async def test_every_label_that_asks_for_nothing_is_read_the_same(label: str) -> None:
    with reading_presses("type", label, None), pytest.raises(PressNeedsApproval) as stopped:
        await _read(_ids(PAY))

    assert stopped.value.action == "type" and "this type" in stopped.value.message


@pytest.mark.parametrize("answer", [CLOSE, None, {"tag": "a", "labels": ["Delete your account"]}, "button", 7])
async def test_what_is_no_button_of_the_table_is_pressed(answer: Any) -> None:
    with reading_presses("click", "read", None):
        await _read(_ids(answer))


@pytest.mark.parametrize(
    "failure",
    [BrowserActionError("Delete account <b>now</b>", code=PAGE_NOT_RESPONDING), RuntimeError("Delete account")],
)
async def test_what_cannot_be_read_is_pressed_and_logged_without_the_pages_text(
    failure: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="app.browser.click_target")

    with reading_presses("click", "read", None):
        await _read(_ids(failure))

    info = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert len(info) == 1 and "session-7" in info[0] and type(failure).__name__ in info[0]
    assert "Delete account" not in info[0]


def _service(under_the_pointer: Any) -> tuple[BrowserActionService, SimpleNamespace, list[str]]:
    """An action service on a fake page: what its mouse and its locator did, in order."""
    done: list[str] = []

    async def note(what: str) -> None:
        done.append(what)

    mouse = SimpleNamespace(move=AsyncMock(), down=lambda: note("mouse down"), up=lambda: note("mouse up"))
    manager = SimpleNamespace(
        settings=SimpleNamespace(default_viewport_width=1280, default_viewport_height=720, action_timeout_ms=15000),
        element_ids=_ids(under_the_pointer),
    )
    session = SimpleNamespace(id="session-7", page=SimpleNamespace(mouse=mouse), mouse_position=(10.0, 10.0))
    return BrowserActionService(manager), session, done


BOX = {"x": 20, "y": 30, "width": 40, "height": 40}
# Where the page says Playwright's click lands, from the corner of the padding box, and the border at that corner.
PLACE = {"x": 12, "y": 7, "border": [3, 2], "boxless": False, "contents": False, "framed": False, "counted": False}
PLACED_AT = (20 + 3 + 12.0, 30 + 2 + 7.0)


def _locator(
    done: list[str],
    *,
    box: Any = BOX,
    place: dict[str, Any] = PLACE,
    enabled: tuple[Any, ...] = (True,),
    each_step_takes: float = 0.0,
    under_the_pointer: Any = True,
) -> SimpleNamespace:
    """A locator whose steps note what they were given. ``enabled`` is what each look at the target finds (an
    exception is raised), the last one for good; ``each_step_takes`` is the time a step holds the click for (the
    clock is the real one); ``under_the_pointer`` is what the page answers when asked whether the pointer finds
    the target (an exception is raised)."""
    budgets: dict[str, Any] = {}
    looks = list(enabled)

    def given_budget(step: str, given: dict[str, Any]) -> None:
        budgets.setdefault(step, given.get("timeout"))
        time.sleep(each_step_takes)

    async def is_enabled(**given: Any) -> bool:
        given_budget("enabled", given)
        done.append("is it enabled")
        look = looks.pop(0) if len(looks) > 1 else looks[0]
        if isinstance(look, BaseException):
            raise look
        return look

    async def hover(**given: Any) -> None:
        if given.get("trial"):
            given_budget("trial hover", given)
            done.append("trial hover")
            return
        given_budget("hover", given)
        done.append(f"hover at {given['position']}")

    async def click(**given: Any) -> None:
        given_budget("trial click" if given.get("trial") else "click", given)
        done.append("trial click" if given.get("trial") else "click")

    async def evaluate(script: str, *_args: Any, **given: Any) -> Any:
        if script == _RECEIVES_POINTER_SCRIPT:
            given_budget("pointer", given)
            done.append(ASKED)
            if isinstance(under_the_pointer, BaseException):
                raise under_the_pointer
            return under_the_pointer
        assert script == _PRESS_PLACE_SCRIPT
        given_budget("place", given)
        return place

    async def bounding_box(**given: Any) -> Any:
        # Read twice by a placed click: for the place to be counted from, and again once the hover has put
        # the pointer there.
        given_budget("box again" if "box" in budgets else "box", given)
        return box

    return SimpleNamespace(
        hover=hover,
        click=click,
        is_enabled=is_enabled,
        evaluate=evaluate,
        bounding_box=bounding_box,
        budgets=budgets,
    )


# Playwright's checks of a click with nothing pressed. Never a trial click: that one presses (the mouse goes down
# and up for real, cancelled in the target's own frame only), which the real-browser tests show over a frame.
CHECKS = ["is it enabled", "trial hover"]
# The question a placed click asks once the pointer is where Playwright's hover put it.
ASKED = "does the pointer find it"


async def test_the_mouse_does_not_go_down_on_a_button_nobody_was_asked_about() -> None:
    service, session, done = _service(DELETE)

    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        with reading_presses("click", "read", None), pytest.raises(PressNeedsApproval):
            await service.press_mouse(session, 40.0, 50.0)
        assert done == []

        # Outside a read action (the click route, a governed run under its approval) the press is the caller's.
        await service.press_mouse(session, 40.0, 50.0)
    assert done == ["mouse down", "mouse up"]
    assert service.manager.element_ids.evaluate.await_count == 1


async def test_a_click_playwright_places_is_pressed_by_the_controller_where_it_was_read() -> None:
    # Playwright's click picks its own point and presses in one step. For a read action only the placing is left
    # to it: its checks (with no click in them), then its hover at a position the controller gave it. The pointer
    # is put on that very point, and the press is press_mouse's: read there, then down.
    placed = "hover at {'x': 12.0, 'y': 7.0}"
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, done = _service(CLOSE)
        with reading_presses("click", "read", None):
            await service.click_as_playwright_places_it(session, _locator(done), budget=_Budget(1000))
        assert done == [*CHECKS, placed, ASKED, "mouse down", "mouse up"]
        session.page.mouse.move.assert_awaited_once_with(*PLACED_AT)
        arg = service.manager.element_ids.evaluate.await_args.args[2]
        assert (arg["x"], arg["y"]) == PLACED_AT

        service, session, done = _service(DELETE)
        with reading_presses("click", "read", None), pytest.raises(PressNeedsApproval):
            await service.click_as_playwright_places_it(session, _locator(done))
        assert done == [*CHECKS, placed, ASKED]

        # Where the pointer was placed it does not find the target (taken away since the hover, or under something
        # the hover could not see): nothing is read and nothing pressed. Whatever is no plain yes is a no.
        for answer in (False, "framed", None):
            service, session, done = _service(CLOSE)
            with reading_presses("click", "read", None), pytest.raises(BrowserActionError) as refused:
                await service.click_as_playwright_places_it(session, _locator(done, under_the_pointer=answer))
            assert (refused.value.code, refused.value.retryable) == ("target_moved", True), answer
            assert done == [*CHECKS, placed, ASKED], answer
            service.manager.element_ids.evaluate.assert_not_awaited()

        # Not read: Playwright's click alone, as before.
        service, session, done = _service(DELETE)
        await service.click_as_playwright_places_it(session, _locator(done))
        assert done == ["click"]
        service.manager.element_ids.evaluate.assert_not_awaited()

        # A target with no box of its own has no point to count from: refused before the pointer is put anywhere.
        service, session, done = _service(CLOSE)
        boxless = _locator(done, box=None, place={**PLACE, "boxless": True, "contents": True})
        with reading_presses("click", "read", None), pytest.raises(BrowserActionError) as refused:
            await service.click_as_playwright_places_it(session, boxless)
        assert refused.value.code == "target_has_no_box"
        assert done == CHECKS

        # One that has no box because it is not shown any more (hidden after the checks passed) has none inside it
        # to name either: it was told "Name an element inside it", not retryable. It has left, which is retryable.
        service, session, done = _service(CLOSE)
        hidden = _locator(done, box=None, place={**PLACE, "boxless": True, "contents": False})
        with reading_presses("click", "read", None), pytest.raises(BrowserActionError) as refused:
            await service.click_as_playwright_places_it(session, hidden)
        assert (refused.value.code, refused.value.retryable) == ("target_moved", True)
        assert done == CHECKS


async def test_the_steps_of_a_placed_click_share_the_clicks_budget() -> None:
    # The read of the place and the read of the box had no bound of their own: under a short budget (a focus
    # click's) a target that went away between two steps waited out Playwright's default instead. Then each step
    # had the whole budget, and a target that kept every one of them waiting held its session for five budgets:
    # a click has no other bound. Each step is given what the steps before it left.
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, done = _service(CLOSE)
        locator = _locator(done, each_step_takes=0.02)
        with reading_presses("click", "read", None):
            await service.click_as_playwright_places_it(session, locator, budget=_Budget(250))
        # The last is the question whether the pointer finds the target where it was put: its first message had
        # no bound of its own either, so a target that had left by then was waited for a whole default more.
        assert list(locator.budgets) == ["enabled", "trial hover", "box", "place", "hover", "box again", "pointer"]
        given = list(locator.budgets.values())
        # Never more than the step before had (a clock may not move in 20 ms), and the last has less than the first.
        assert given == sorted(given, reverse=True) and 0 < given[-1] < given[0] <= 250, given

        # With nothing left a step has a millisecond, never 0: Playwright reads 0 as no limit at all.
        service, session, done = _service(CLOSE)
        locator = _locator(done, each_step_takes=0.02)
        with reading_presses("click", "read", None):
            await service.click_as_playwright_places_it(session, locator, budget=_Budget(30))
        assert (locator.budgets["box again"], locator.budgets["pointer"]) == (1, 1), locator.budgets

        # No budget named: the session's own (ACTION_TIMEOUT_MS), which is what Playwright gives a step left to
        # itself, and one deadline for the steps as well. The question has its own bound within that.
        service, session, done = _service(CLOSE)
        locator = _locator(done)
        with reading_presses("click", "read", None):
            await service.click_as_playwright_places_it(session, locator)
        assert 14000 < locator.budgets["box again"] <= locator.budgets["enabled"] <= 15000, locator.budgets
        assert locator.budgets["pointer"] == POINTER_CHECK_TIMEOUT_MS

        # Not read, the click is Playwright's own, and its timeout is the budget.
        service, session, done = _service(CLOSE)
        locator = _locator(done)
        await service.click_as_playwright_places_it(session, locator, budget=_Budget(250))
        assert done == ["click"] and 200 < locator.budgets["click"] <= 250, locator.budgets


@pytest.mark.parametrize("time_left", [False, True], ids=["the click's time spent", "the click's time not spent"])
async def test_a_placed_clicks_last_question_unanswered_in_its_time_ends_the_click_as_a_timeout(
    time_left: bool,
) -> None:
    # The question asked with the pointer placed has what the click has left, a second at most, and a question that
    # went unanswered read as a no: the caller was told the target had moved (target_moved, "the pointer does not
    # find the target"), where the page had not answered, as any step before the question would have said. Told
    # apart by the click's time alone, that held only where the time was spent; a page busy at the question with
    # more than a second of the click left ended as target_moved after its second.
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, done = _service(CLOSE)
        unanswered = PlaywrightTimeoutError("Locator.evaluate: Timeout 1000ms exceeded.")
        late = _locator(done, each_step_takes=0.0 if time_left else 0.02, under_the_pointer=unanswered)
        with reading_presses("click", "read", None), pytest.raises(PlaywrightTimeoutError) as ended:
            await service.click_as_playwright_places_it(session, late, budget=_Budget(5000 if time_left else 30))
        assert ended.value is unanswered
        assert done[-1] == ASKED and "mouse down" not in done, done
        assert late.budgets["pointer"] == (POINTER_CHECK_TIMEOUT_MS if time_left else 1), late.budgets
        service.manager.element_ids.evaluate.assert_not_awaited()


async def test_a_no_where_the_pointer_was_placed_is_target_moved_whatever_the_click_has_left() -> None:
    # A no is the page's answer, given in time: the target was taken away or covered once the hover had passed.
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, done = _service(CLOSE)
        refused = _locator(done, each_step_takes=0.02, under_the_pointer=False)
        with reading_presses("click", "read", None), pytest.raises(BrowserActionError) as ended:
            await service.click_as_playwright_places_it(session, refused, budget=_Budget(30))
        assert (ended.value.code, ended.value.retryable) == ("target_moved", True)


@pytest.mark.parametrize("read", [False, True])
async def test_a_click_playwright_places_after_a_human_like_try_has_what_the_try_left(read: bool) -> None:
    # The placed click began a budget of its own, or gave Playwright's click the whole one again: a target that
    # kept the checks waiting and then the placing held its session for two budgets. A click has one, made where
    # it begins and handed down. Each step here takes about 50 ms (the clock moves in steps of some 16 ms).
    service, session, done = _service(CLOSE)
    locator = _locator(done, each_step_takes=0.05)
    service.move_mouse_human_like = AsyncMock()
    service.pause_before_press = AsyncMock()
    service._receives_pointer_at = AsyncMock(side_effect=[False, True])
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        if read:
            with reading_presses("click", "read", None):
                await service.click_locator_human_like(session, locator, (40.0, 50.0), timeout_ms=1000)
        else:
            await service.click_locator_human_like(session, locator, (40.0, 50.0), timeout_ms=1000)
    if read:
        # The try's two checks, then the placed click's two: four steps before the place is read, where a
        # budget of its own had lost two.
        assert done == [*CHECKS, *CHECKS, "hover at {'x': 12.0, 'y': 7.0}", "mouse down", "mouse up"]
        assert 0 < locator.budgets["place"] <= 1000 - 3 * 50, locator.budgets
    else:
        # Two steps before Playwright's click, which had the whole budget again.
        assert done == [*CHECKS, "click"]
        assert 0 < locator.budgets["click"] <= 1000 - 50, locator.budgets


@pytest.mark.parametrize("read", [False, True])
async def test_a_click_whose_box_cannot_be_read_after_its_scroll_hands_the_placed_click_what_is_left(
    read: bool,
) -> None:
    # The click's one budget begins before its scroll. Where the box then cannot be read (the target hidden or taken
    # away between the two), the click is one Playwright places, and that began a whole budget of its own: such a
    # click held its session for the scroll's wait and a whole limit more. Each step here takes about 50 ms.
    service, session, done = _service(CLOSE)
    service.manager.settings.action_timeout_ms = 1000
    locator = _locator(done, box=None, each_step_takes=0.05)

    async def scroll_into_view_if_needed(**given: Any) -> None:
        locator.budgets.setdefault("scroll", given.get("timeout"))
        time.sleep(0.05)

    async def run_action(_session: Any, _action: str, _target: Any, operation: Any) -> dict[str, Any]:
        await operation()
        return {}

    locator.scroll_into_view_if_needed = scroll_into_view_if_needed
    session.page.locator = lambda _selector: SimpleNamespace(first=locator)
    service.manager.get_session = AsyncMock(return_value=session)
    service.manager._settle = AsyncMock()
    service.manager._run_action = run_action
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        if read:
            with reading_presses("click", "read", None), pytest.raises(BrowserActionError):
                await service.click("session-7", selector="#t")
        else:
            await service.click("session-7", selector="#t")
    # Its first step: the look whether the target is enabled for a read click, Playwright's own click otherwise. The
    # scroll and the read of the box came before it, about 100 ms (the clock moves in steps of some 16 ms).
    first = locator.budgets["enabled" if read else "click"]
    assert 0 < first <= 1000 - 50, locator.budgets


async def test_the_last_questions_before_a_human_like_press_are_bounded_by_the_clicks_budget() -> None:
    # What the pointer finds, and whether the target is enabled still: a second each at most, and never more than
    # the click has left.
    service, session, done = _service(CLOSE)
    locator = _locator(done, each_step_takes=0.05)
    service.move_mouse_human_like = AsyncMock()
    service.pause_before_press = AsyncMock()
    asked: list[Any] = []
    real = locator.is_enabled

    async def is_enabled(**given: Any) -> bool:
        asked.append(given.get("timeout"))
        return await real(**given)

    locator.is_enabled = is_enabled
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        await service.click_locator_human_like(session, locator, (40.0, 50.0), timeout_ms=1000)
    assert done == [*CHECKS, ASKED, "is it enabled", "mouse down", "mouse up"]
    assert 0 < asked[1] <= locator.budgets["pointer"] <= 1000 - 50, (asked, locator.budgets)

    service, session, done = _service(CLOSE)
    locator = _locator(done)
    service.move_mouse_human_like = AsyncMock()
    service.pause_before_press = AsyncMock()
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        await service.click_locator_human_like(session, locator, (40.0, 50.0))
    assert locator.budgets["pointer"] == POINTER_CHECK_TIMEOUT_MS


async def test_the_checks_before_a_human_like_press_are_no_click_either() -> None:
    # The first step of every selector click: it was a trial click too.
    service, session, done = _service(CLOSE)
    locator = _locator(done)
    service.move_mouse_human_like = AsyncMock()
    service.pause_before_press = AsyncMock()
    service._receives_pointer_at = AsyncMock(return_value=True)
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        await service.click_locator_human_like(session, locator, (40.0, 50.0), timeout_ms=250)
    # Enabled is asked once more where the press is about to be made: the pointer was a while on its way.
    assert done == [*CHECKS, "is it enabled", "mouse down", "mouse up"]
    assert set(locator.budgets) == {"enabled", "trial hover"}
    assert all(200 < budget <= 250 for budget in locator.budgets.values()), locator.budgets


@pytest.mark.parametrize("at_the_press", [False, PlaywrightTimeoutError("the target has left")])
async def test_a_target_that_is_not_enabled_when_the_pointer_arrives_is_not_pressed_there(at_the_press: Any) -> None:
    # It was: a mouse down on a button the page had turned off since the checks, and the action ended as done.
    # The click is handed to Playwright's placing, whose checks wait for the target. What cannot be told is a no.
    service, session, done = _service(CLOSE)
    locator = _locator(done, enabled=(True, at_the_press, True))
    service.move_mouse_human_like = AsyncMock()
    service.pause_before_press = AsyncMock()
    service._receives_pointer_at = AsyncMock(return_value=True)
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        await service.click_locator_human_like(session, locator, (40.0, 50.0), timeout_ms=250)
    assert done == [*CHECKS, "is it enabled", "click"]


async def test_a_disabled_target_is_looked_at_again_until_it_is_enabled() -> None:
    # Each look finds the element the selector names then: the one a page drew in the first one's place too.
    service, session, done = _service(CLOSE)
    locator = _locator(done, enabled=(False, False, True))
    service.move_mouse_human_like = AsyncMock()
    service.pause_before_press = AsyncMock()
    service._receives_pointer_at = AsyncMock(return_value=True)
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()) as waited:
        await service.click_locator_human_like(session, locator, (40.0, 50.0), timeout_ms=250)
    assert done == [*["is it enabled"] * 3, "trial hover", "is it enabled", "mouse down", "mouse up"]
    assert [call.args for call in waited.await_args_list[:2]] == [(ENABLED_POLL_SECONDS,)] * 2


async def test_a_target_that_stays_disabled_ends_the_click_when_its_budget_does() -> None:
    service, session, done = _service(CLOSE)
    locator = _locator(done, enabled=(False,), each_step_takes=0.02)
    with (
        patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()),
        pytest.raises(PlaywrightTimeoutError, match="enabled"),
    ):
        await service.click_locator_human_like(session, locator, (40.0, 50.0), timeout_ms=500)
    # 20 ms a look under a budget of 500: looked at more than once, also where a look is held up for ten
    # times as long (under a budget of 50 one stall of 30 ms left it at a single look), and nothing else done.
    assert len(done) >= 2 and set(done) == {"is it enabled"}, done


def _field(done: list[str], **given: Any) -> SimpleNamespace:
    """A field whose focus click goes as ``_locator``'s click does, and which ``focus()`` puts the caret in."""
    field = _locator(done, **given)
    focused = {"now": False}
    placed_or_asked = field.evaluate

    async def evaluate(script: str, *args: Any, **named: Any) -> Any:
        if script == ELEMENT_HAS_FOCUS_SCRIPT:
            return focused["now"]
        return await placed_or_asked(script, *args, **named)

    async def focus(**_given: Any) -> None:
        done.append("focus")
        focused["now"] = True

    field.evaluate, field.focus = evaluate, focus
    return field


@pytest.mark.parametrize(
    "refused",
    [
        pytest.param({"under_the_pointer": False}, id="the pointer does not find it where the click was placed"),
        pytest.param({"box": None}, id="it has no box once the pointer is on it"),
    ],
)
async def test_a_types_focus_click_refused_where_it_was_placed_focuses_its_field_with_no_press(
    refused: dict[str, Any],
) -> None:
    # 1.17.0 focused the field where Playwright's own click was refused (a PlaywrightError). For a type whose target
    # is read the placed click is the controller's own, and its refusal (target_moved) ended the type, which a retry
    # met again: nothing was pressed, and nothing was typed either.
    service, session, done = _service(CLOSE)
    field = _field(done, **refused)
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()), reading_presses("type", "write", None):
        await service.focus_verified(session, field, action="type")
    assert done[-1] == "focus" and "mouse down" not in done, done
    service.manager.element_ids.evaluate.assert_not_awaited()


@pytest.mark.parametrize(
    ("given", "code"),
    [
        pytest.param({}, "press_needs_approval", id="a button of the table under the pointer"),
        pytest.param({"box": None, "place": {**PLACE, "boxless": True, "contents": True}}, "target_has_no_box"),
    ],
)
async def test_a_types_focus_click_that_asks_or_has_no_box_still_ends_the_type(
    given: dict[str, Any], code: str
) -> None:
    # Only target_moved is a focus click that pressed nothing and may be focused instead: one stopped for an approval
    # asks for it, and a field with no box of its own is named as such, as before.
    service, session, done = _service(DELETE)
    field = _field(done, **given)
    with (
        patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()),
        reading_presses("type", "write", None),
        pytest.raises(BrowserActionError) as ended,
    ):
        await service.focus_verified(session, field, action="type")
    assert ended.value.code == code
    assert "focus" not in done and "mouse down" not in done, done


async def test_a_target_with_no_box_once_the_pointer_is_on_it_is_not_pressed() -> None:
    # Before, an element with no box once the pointer was there was clicked unread.
    service, session, done = _service(CLOSE)
    with reading_presses("click", "read", None), pytest.raises(BrowserActionError) as refused:
        await service.click_as_playwright_places_it(session, _locator(done, box=None))
    assert (refused.value.code, refused.value.retryable) == ("target_moved", True)
    assert "mouse down" not in done and "click" not in done
    service.manager.element_ids.evaluate.assert_not_awaited()


async def test_the_press_read_is_short() -> None:
    # The pointer is there, waiting: a page that does not answer costs a press this much, twice at most.
    assert click_target.PRESS_READ_TIMEOUT_MS <= 500


@pytest.mark.parametrize("placed", [False, True])
async def test_only_the_target_of_a_placed_click_is_asked_as_the_button_or_link_it_is_in(placed: bool) -> None:
    # The question's last argument: whether the pointer may find the button or the link the node is in, as
    # Playwright's own check of a click allows its target. Of the target where the click was placed, and of no
    # frame on the way up: a frame must itself be what the pointer finds, or the press goes to the page over it.
    asked: list[tuple[str, Any]] = []

    def node(name: str, frame: Any) -> SimpleNamespace:
        async def evaluate(script: str, given: list[Any]) -> bool:
            assert script == _RECEIVES_POINTER_SCRIPT
            asked.append((name, given[-1]))
            return True

        return SimpleNamespace(
            evaluate=evaluate,
            bounding_box=AsyncMock(return_value={"x": 1, "y": 2, "width": 30, "height": 40}),
            owner_frame=AsyncMock(return_value=frame),
            dispose=AsyncMock(),
        )

    outer = node("the outer frame", SimpleNamespace(parent_frame=None))
    inner = node("the inner frame", SimpleNamespace(parent_frame=object(), frame_element=AsyncMock(return_value=outer)))
    target = node("the target", SimpleNamespace(parent_frame=object(), frame_element=AsyncMock(return_value=inner)))

    async def first(script: str, given: list[Any], **_named: Any) -> str:
        asked.append(("the target, first", given[-1]))
        return "framed"

    locator = SimpleNamespace(evaluate=first, element_handle=AsyncMock(return_value=target))

    assert await BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=placed) is True
    assert asked == [
        ("the target, first", placed),
        ("the target", placed),
        ("the inner frame", False),
        ("the outer frame", False),
    ]


async def test_a_place_counted_from_a_box_that_changes_at_every_read_is_counted_again_a_bounded_number_of_times() -> (
    None
):
    # Counted from Playwright's box, the place is counted again where that box changed while the script ran. A
    # target the page changes at every read (an animation) is not waited on: the last place counted is hovered at.
    boxes = iter({"x": float(n), "y": 0.0, "width": 10.0, "height": 10.0} for n in range(100))
    counts: list[Any] = []

    async def bounding_box(**_given: Any) -> Any:
        return next(boxes)

    async def evaluate(script: str, given: list[Any], **_named: Any) -> dict[str, Any]:
        assert script == _PRESS_PLACE_SCRIPT
        counts.append(given[0]["x"])
        return {**PLACE, "counted": True}

    locator = SimpleNamespace(bounding_box=bounding_box, evaluate=evaluate)
    seen, place = await BrowserActionService._place_of(locator, _Budget(5000))

    assert counts == [float(n) for n in range(PLACE_RECOUNTS + 1)]
    assert seen["x"] == PLACE_RECOUNTS and place["counted"] is True


async def test_a_place_counted_from_a_box_that_stood_is_counted_once() -> None:
    reads: list[str] = []

    async def bounding_box(**_given: Any) -> Any:
        reads.append("box")
        return {"x": 1.0, "y": 2.0, "width": 10.0, "height": 10.0}

    async def evaluate(script: str, given: list[Any], **_named: Any) -> dict[str, Any]:
        reads.append("place")
        return {**PLACE, "counted": True}

    await BrowserActionService._place_of(SimpleNamespace(bounding_box=bounding_box, evaluate=evaluate), _Budget(5000))
    assert reads == ["box", "place", "box"]


def _framed_node(name: str, frame: Any, asked: list[Any], record: Any = None, answer: Any = True) -> SimpleNamespace:
    """A handle on an element in a frame: what it is asked, and, read with a watch, the record ``record``."""

    async def evaluate(script: str, given: Any) -> Any:
        if script != _RECEIVES_POINTER_SCRIPT:
            assert "seen.at" in script, script
            if isinstance(record, BaseException):
                raise record
            return record
        asked.append((name, given))
        return answer

    return SimpleNamespace(
        evaluate=evaluate,
        bounding_box=AsyncMock(return_value=BOX),
        owner_frame=AsyncMock(return_value=frame),
        dispose=AsyncMock(),
    )


async def test_a_placed_click_in_a_frame_asks_its_target_where_its_document_saw_the_pointer_arrive() -> None:
    # The point of the question was carried into a frame by the target's two boxes, and Playwright's box of an
    # element in a frame of another process leaves out the frame's border and padding: the question was asked
    # that far from the pointer. The target's document is asked where it saw the pointer's move arrive, and the
    # target is asked at that point, a point of its own document (``true`` for its box: nothing is carried). The
    # frame above is asked as before, at the point of the main frame carried by its own two boxes.
    asked: list[tuple[str, list[Any]]] = []
    outer = _framed_node("the frame", SimpleNamespace(parent_frame=None), asked)
    up = SimpleNamespace(parent_frame=object(), frame_element=AsyncMock(return_value=outer))
    target = _framed_node("the target", up, asked, record=[7.5, 8.25])
    watch = SimpleNamespace(dispose=AsyncMock())
    locator = SimpleNamespace(evaluate=AsyncMock(return_value="framed"), element_handle=AsyncMock(return_value=target))

    found = await BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=True, watch=watch, placed_by=BOX)
    assert found is True
    assert asked == [("the target", [7.5, 8.25, True, False, True]), ("the frame", [5.0, 6.0, BOX, False, False])]
    # The record says the target is in a frame, and no box of the target carries anything: the target is not looked
    # for again, and its box is read once, to tell that it is where the click was placed by.
    locator.evaluate.assert_not_awaited()
    target.bounding_box.assert_awaited_once()
    # The watch is the caller's to give back; the handles the question took are its own.
    watch.dispose.assert_not_awaited()
    target.dispose.assert_awaited_once()
    outer.dispose.assert_awaited_once()


@pytest.mark.parametrize("along", ["x", "y", "width", "height"])
@pytest.mark.parametrize(("moved_by", "found"), [(0.5, True), (0.6, False)])
async def test_a_record_is_not_taken_where_the_target_has_moved_since_the_click_was_placed(
    along: str, moved_by: float, found: bool
) -> None:
    # The record is the point where the target's document saw the pointer arrive. A page that moves the frame after
    # that move (on it) leaves the pointer, which does not move, over another point of that document, and the record
    # does not say so: asked at it, the question found the card, and the press went down on what had come under
    # the pointer. The target's box must still be the one the click was placed by, to half a pixel; else no. Its
    # size as well: a frame scaled about the target's corner keeps the corner of its box.
    asked: list[tuple[str, list[Any]]] = []
    outer = _framed_node("the frame", SimpleNamespace(parent_frame=None), asked)
    up = SimpleNamespace(parent_frame=object(), frame_element=AsyncMock(return_value=outer))
    target = _framed_node("the target", up, asked, record=[7.5, 8.25])
    target.bounding_box = AsyncMock(return_value={**BOX, along: BOX[along] + moved_by})
    watch = SimpleNamespace(dispose=AsyncMock())
    locator = SimpleNamespace(evaluate=AsyncMock(return_value="framed"), element_handle=AsyncMock(return_value=target))

    answer = await BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=True, watch=watch, placed_by=BOX)
    assert answer is found
    assert [name for name, _ in asked] == (["the target", "the frame"] if found else [])
    target.dispose.assert_awaited_once()


@pytest.mark.parametrize("a_frame_of_the_target_is_there", [True, False])
async def test_a_placed_click_whose_move_its_targets_document_never_saw_is_asked_only_for_a_frame_of_the_target(
    a_frame_of_the_target_is_there: bool,
) -> None:
    # No record: the pointer's move went to another document. Over a frame inside the target (a video in a card),
    # which Playwright's own check of a click takes for its target, the click is pressed, as it was: the question
    # is asked by the two boxes, and only such a frame is a yes. Anything else (the page over the frame, a frame
    # over the target, a frame the page put under the pointer) is a no.
    asked: list[tuple[str, list[Any]]] = []
    outer = _framed_node("the frame", SimpleNamespace(parent_frame=None), asked)
    up = SimpleNamespace(parent_frame=object(), frame_element=AsyncMock(return_value=outer))
    target = _framed_node("the target", up, asked, record=None, answer=a_frame_of_the_target_is_there)
    watch = SimpleNamespace(dispose=AsyncMock())
    locator = SimpleNamespace(evaluate=AsyncMock(return_value="framed"), element_handle=AsyncMock(return_value=target))

    found = await BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=True, watch=watch)
    assert found is a_frame_of_the_target_is_there
    walked = [("the frame", [5.0, 6.0, BOX, False, False])] if a_frame_of_the_target_is_there else []
    assert asked == [("the target", [5.0, 6.0, BOX, False, True, True]), *walked]
    locator.evaluate.assert_not_awaited()


async def test_a_placed_click_in_a_frame_that_runs_no_script_is_asked_by_the_targets_two_boxes() -> None:
    # A document that runs no script (a sandboxed frame without allow-scripts) runs no listener either: its watch
    # says so (``false``), no record can come, and the question is carried by the two boxes, as it was before.
    asked: list[tuple[str, list[Any]]] = []
    target = _framed_node("the target", SimpleNamespace(parent_frame=None), asked, record=False)
    locator = SimpleNamespace(evaluate=AsyncMock(return_value="framed"), element_handle=AsyncMock(return_value=target))

    watch = SimpleNamespace(dispose=AsyncMock())
    assert await BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=True, watch=watch) is True
    assert asked == [("the target", [5.0, 6.0, BOX, False, True])]
    locator.evaluate.assert_not_awaited()


async def test_a_question_cancelled_as_it_gives_a_handle_back_still_gives_the_others_back() -> None:
    # The question gave its handles back in one loop that let only an Exception by: a cancellation at the target's
    # skipped the frame's above it, which the page then kept for good. Each is given back, and the cancellation is
    # raised after.
    asked: list[tuple[str, list[Any]]] = []
    outer = _framed_node("the frame", SimpleNamespace(parent_frame=None), asked)
    up = SimpleNamespace(parent_frame=object(), frame_element=AsyncMock(return_value=outer))
    target = _framed_node("the target", up, asked, record=[7.5, 8.25])
    target.dispose = AsyncMock(side_effect=asyncio.CancelledError)
    watch = SimpleNamespace(dispose=AsyncMock())
    locator = SimpleNamespace(evaluate=AsyncMock(return_value="framed"), element_handle=AsyncMock(return_value=target))

    with pytest.raises(asyncio.CancelledError):
        await BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=True, watch=watch, placed_by=BOX)
    target.dispose.assert_awaited_once()
    outer.dispose.assert_awaited_once()


async def test_a_watch_the_targets_document_cannot_read_is_a_no() -> None:
    # The record is read in the target's own document, which the driver refuses for a handle of another (the
    # selector found an element in another document since, or the frame went to another page): a no, and not the
    # timeout a question that goes unanswered ends a placed click with.
    asked: list[tuple[str, list[Any]]] = []
    refused = PlaywrightError("JSHandles can be evaluated only in the context they were created!")
    target = _framed_node("the target", SimpleNamespace(parent_frame=None), asked, record=refused)
    locator = SimpleNamespace(evaluate=AsyncMock(), element_handle=AsyncMock(return_value=target))

    watch = SimpleNamespace(dispose=AsyncMock())
    assert await BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=True, watch=watch) is False
    assert asked == []
    target.dispose.assert_awaited_once()


def _framed_click(
    framed: bool, box: Any = BOX, stalls: str | None = None
) -> tuple[Any, Any, Any, SimpleNamespace, list[str]]:
    """A read click Playwright places, on a target in a frame or not, whose handles note what is done with them. The
    step ``stalls`` names is noted, and then never answers."""
    service, session, done = _service(CLOSE)
    locator = _locator(done, place={**PLACE, "framed": framed})

    async def note(what: str, value: Any = None) -> Any:
        done.append(what)
        if what == stalls:
            await _never()
        return value

    async def stop(script: str) -> None:
        assert "stop()" in script, script
        await note("stop the watch")

    watch = SimpleNamespace(evaluate=stop, dispose=lambda: note("give the watch back"))

    async def evaluate_handle(script: str) -> Any:
        assert "pointermove" in script
        return await note("watch the pointer", watch)

    target = SimpleNamespace(
        bounding_box=lambda: note("the target's box", box),
        evaluate_handle=evaluate_handle,
        dispose=lambda: note("give the target back"),
    )
    locator.element_handle = lambda **_given: note("look for the target", target)
    session.page.locator = lambda _selector: SimpleNamespace(evaluate=AsyncMock(return_value=[1280, 720]))
    service._receives_pointer_at = AsyncMock(return_value=True)
    return service, session, locator, watch, done


def _assert_given_back(steps: list[str] | None) -> None:
    """The steps of a placed click's give-back in a frame (None: one not in a frame, which has none): the watch
    stopped, then given back, and the target given back beside it (B1-fix1 item 12: together, not in turn)."""
    if steps is None:
        return
    assert sorted(steps) == ["give the target back", "give the watch back", "stop the watch"], steps
    assert steps.index("stop the watch") < steps.index("give the watch back"), steps


@pytest.mark.parametrize("framed", [True, False])
async def test_a_placed_click_in_a_frame_sets_its_watch_up_between_playwrights_hover_and_its_own_move(
    framed: bool,
) -> None:
    # After the hover, or the record would be of the hover's move; before the controller's own move to the same
    # point, which is the one the record is of. A target in the main frame needs none: (x, y) is its point. The
    # target is looked for once, for its box and the watch, and both are given back after the press.
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, locator, watch, done = _framed_click(framed)

        async def move(x: float, y: float) -> None:
            done.append(f"the pointer is put at {(x, y)}")

        session.page.mouse.move = move
        with reading_presses("click", "read", None):
            await service.click_as_playwright_places_it(session, locator, budget=_Budget(1000))
    put = f"the pointer is put at {PLACED_AT}"
    looked = ["look for the target", "the target's box", "watch the pointer"] if framed else []
    pressed = [*CHECKS, "hover at {'x': 12.0, 'y': 7.0}", *looked, put, "mouse down", "mouse up"]
    assert done[: len(pressed)] == pressed, done
    _assert_given_back(done[len(pressed) :] if framed else None)
    asked = service._receives_pointer_at.await_args.kwargs
    assert asked["watch"] is (watch if framed else None)
    # The box the click was placed by, which the target's must still be where a record is taken.
    assert asked["placed_by"] == BOX


async def test_the_watch_of_a_placed_click_is_given_back_when_the_click_ends_before_its_question() -> None:
    # The record and the target are handles the page keeps until they are given back: a click that ended between
    # setting the watch up and asking (the page went away under the move) left both with the page for good.
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, locator, watch, done = _framed_click(True)
        session.page.mouse.move = AsyncMock(side_effect=RuntimeError("the page has gone"))
        with reading_presses("click", "read", None), pytest.raises(RuntimeError, match="the page has gone"):
            await service.click_as_playwright_places_it(session, locator, budget=_Budget(1000))
    _assert_given_back(done[-3:])
    service._receives_pointer_at.assert_not_awaited()
    assert "mouse down" not in done


async def test_the_watch_of_a_placed_click_is_stopped_where_its_record_was_never_read() -> None:
    # Reading the record ends the watch. A question that ended before it read the record (no move came to the
    # target's document, and the target's second look-up failed) left the watch's listeners on the frame's window.
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, locator, watch, done = _framed_click(True)
        service._receives_pointer_at = AsyncMock(return_value=False)
        with reading_presses("click", "read", None), pytest.raises(BrowserActionError) as ended:
            await service.click_as_playwright_places_it(session, locator, budget=_Budget(1000))
    assert (ended.value.code, ended.value.retryable) == ("target_moved", True)
    _assert_given_back(done[-3:])
    assert "mouse down" not in done


async def test_a_click_cancelled_as_it_gives_its_watch_back_still_gives_the_target_back() -> None:
    # A cancellation is no Exception: raised where the watch was given back, it skipped the target's, which the
    # page then kept for good. Each is given back, and the cancellation is raised after.
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, locator, watch, done = _framed_click(True)

        async def cancelled() -> None:
            done.append("give the watch back")
            raise asyncio.CancelledError

        watch.dispose = cancelled
        with reading_presses("click", "read", None), pytest.raises(asyncio.CancelledError):
            await service.click_as_playwright_places_it(session, locator, budget=_Budget(1000))
    # Given back together (B1-fix1 item 12): the watch stopped before it is given back, the target beside it.
    given = done[done.index("mouse up") + 1 :]
    assert sorted(given) == ["give the target back", "give the watch back", "stop the watch"], done
    assert given.index("stop the watch") < given.index("give the watch back"), done


async def test_a_placed_click_in_a_frame_whose_target_has_no_box_sets_no_watch_up() -> None:
    # A watch set up for a click that ended at the box (the target was no longer shown) listened on in the frame
    # until its next move: it is set up once the target has a box. The target is given back.
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, locator, watch, done = _framed_click(True, box=None)
        with reading_presses("click", "read", None), pytest.raises(BrowserActionError) as ended:
            await service.click_as_playwright_places_it(session, locator, budget=_Budget(1000))
    assert (ended.value.code, ended.value.retryable) == ("target_moved", True)
    assert done[-3:] == ["look for the target", "the target's box", "give the target back"]
    service._receives_pointer_at.assert_not_awaited()


async def _never(*_args: Any, **_kwargs: Any) -> Any:
    """A call to a page that does not answer."""
    await asyncio.Event().wait()


# The bound the bound tests below give a click's or a type's waits, and what they allow a wait that meets a page that
# does not answer: three bounds, overhead and all, short of the five a bound five times as long takes (ten passed
# for it, B1-fix1 item 7). A check made twice is allowed five.
BOUND_MS = 200
WITHIN_THREE_BOUNDS_S = 3 * BOUND_MS / 1000
WITHIN_FIVE_BOUNDS_S = 5 * BOUND_MS / 1000


@pytest.mark.parametrize("stalls", ["the target's box", "watch the pointer"])
async def test_a_placed_click_in_a_frame_whose_target_does_not_answer_ends_at_its_budget(stalls: str) -> None:
    # The target's box and the watch set up on it were asked of its handle with no bound: a page that stopped
    # answering there held the click, and its session, past its budget. Each is asked within what the click has
    # left, and past it the click ends as the timeout any of its waits ends with, the target given back.
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, locator, watch, done = _framed_click(True, stalls=stalls)
        began = time.monotonic()
        with reading_presses("click", "read", None), pytest.raises(PlaywrightTimeoutError):
            await asyncio.wait_for(service.click_as_playwright_places_it(session, locator, budget=_Budget(BOUND_MS)), 5)
        took = time.monotonic() - began
    assert took < WITHIN_THREE_BOUNDS_S, took
    assert done[-2:] == [stalls, "give the target back"]
    service._receives_pointer_at.assert_not_awaited()


def _frames_that_stall(stalls: str) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    """A target in a frame, and the frame element above it, one of whose calls (``stalls``) never answers; and the
    locator that finds the target."""
    asked: list[tuple[str, list[Any]]] = []
    outer = _framed_node("the frame", SimpleNamespace(parent_frame=None), asked)
    up = SimpleNamespace(parent_frame=object(), frame_element=AsyncMock(return_value=outer))
    target = _framed_node("the target", up, asked)
    locator = SimpleNamespace(evaluate=AsyncMock(return_value="framed"), element_handle=AsyncMock(return_value=target))
    if stalls == "the-first-question":
        locator.evaluate = AsyncMock(side_effect=_never)
    elif stalls == "frame_element":
        up.frame_element = AsyncMock(side_effect=_never)
    else:
        setattr(target, stalls, _never if stalls == "evaluate" else AsyncMock(side_effect=_never))
    return target, outer, locator


@pytest.mark.parametrize("stalls", ["the-first-question", "bounding_box", "evaluate", "owner_frame", "frame_element"])
@pytest.mark.parametrize("placed", [True, False], ids=["placed", "before-a-human-like-press"])
async def test_a_pointer_question_the_page_does_not_answer_ends_at_its_bound(stalls: str, placed: bool) -> None:
    # Locator.evaluate's timeout bounds only the wait for the element, and the handle calls of the walk up the frames
    # (the target's box, the question, its frame, the frame's element) had no bound at all: a page that stopped
    # answering held the click past its budget. Every call is asked within the question's bound. Past it, where the
    # click was placed, the question raises its timeout (the page did not answer); before a human-like press it is
    # a no, which hands the click to Playwright's placing. The handles taken are given back.
    target, outer, locator = _frames_that_stall(stalls)
    asking = BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=placed, timeout_ms=BOUND_MS)

    began = time.monotonic()
    if placed:
        with pytest.raises(PlaywrightTimeoutError):
            await asyncio.wait_for(asking, 5)
    else:
        assert await asyncio.wait_for(asking, 5) is False
    took = time.monotonic() - began

    assert took < WITHIN_THREE_BOUNDS_S, took
    if stalls != "the-first-question":
        target.dispose.assert_awaited_once()
    assert outer.dispose.await_count == 0


async def _ended_within(call: Awaitable[Any], seconds: float) -> Any:
    """``call``'s answer, where it ends within ``seconds``; a failed test where it does not. Not wait_for: a give-back
    runs on through a cancel until each of its steps is done, so one step with no bound held wait_for's cancel, and the
    test, for good."""
    ending = asyncio.ensure_future(call)
    done, _ = await asyncio.wait({ending}, timeout=seconds)
    assert done, f"not ended within {seconds} s"
    return ending.result()


@pytest.mark.parametrize("stalls", ["the watch's stop", "the watch's stop and its disposal", "the target's disposal"])
async def test_a_placed_clicks_give_back_the_page_does_not_answer_still_gives_each_handle_back(stalls: str) -> None:
    # Each give-back step waited for the page with no bound: a watch whose stop never answered held the click, and
    # its session, with the target never given back. Each step has the bound a give-back has: the target's disposal
    # too (B1-fix1 item 17, A25), and the watch's (item 28, staged F11).
    watch = SimpleNamespace(evaluate=AsyncMock(side_effect=_never if stalls.startswith("the watch's stop") else None))
    watch.dispose = AsyncMock(side_effect=_never if stalls.endswith("and its disposal") else None)
    target = SimpleNamespace(dispose=AsyncMock(side_effect=_never if stalls == "the target's disposal" else None))

    with patch.object(actions, "FIELD_RECHECK_TIMEOUT_MS", BOUND_MS):
        began = time.monotonic()
        await _ended_within(BrowserActionService._give_back(watch, target), 5)
        took = time.monotonic() - began

    assert took < WITHIN_THREE_BOUNDS_S, took
    watch.dispose.assert_awaited_once()
    target.dispose.assert_awaited_once()


async def test_a_focus_check_the_page_does_not_answer_refuses_the_type_within_its_bound() -> None:
    # focus_verified asked the field whether focus was in it with no bound, so a page that stopped answering held the
    # type. A check that does not answer within FIELD_RECHECK_TIMEOUT_MS says no, and the type is refused.
    service, session, _done = _service(CLOSE)
    service.focus_locator = AsyncMock()
    locator = SimpleNamespace(evaluate=AsyncMock(side_effect=_never), focus=AsyncMock())

    with patch.object(actions, "FIELD_RECHECK_TIMEOUT_MS", BOUND_MS):
        began = time.monotonic()
        with pytest.raises(BrowserActionError) as refused:
            await asyncio.wait_for(service.focus_verified(session, locator, action="type"), 5)
        took = time.monotonic() - began

    assert refused.value.code == "focus_lost"
    # Asked twice, after the focus click and after locator.focus(): five bounds.
    assert took < WITHIN_FIVE_BOUNDS_S, took
    locator.focus.assert_awaited_once()


@pytest.mark.parametrize("cancelled_during", ["the pause", "the press"])
async def test_a_press_cancelled_before_its_release_still_lets_the_button_go(cancelled_during: str) -> None:
    # 1b-3: press_mouse sent down(), paused, then up(), and nothing carried the up through a cancel: one in the pause
    # (a client that gave up), or while down() was still unanswered, left the page with its button pressed for
    # whatever ran next. It is let go as a drag's is, and the cancel then goes on.
    reached = asyncio.Event()
    released: list[str] = []

    async def stalls(*_args: Any) -> None:
        reached.set()
        await asyncio.Event().wait()

    async def up() -> None:
        released.append("mouse up")

    mouse = SimpleNamespace(down=AsyncMock(side_effect=stalls if cancelled_during == "the press" else None), up=up)
    session = SimpleNamespace(id="session-7", page=SimpleNamespace(mouse=mouse), mouse_position=(10.0, 10.0))
    service = BrowserActionService(SimpleNamespace())
    pause = stalls if cancelled_during == "the pause" else AsyncMock()

    with patch("app.browser.services.actions.asyncio.sleep", new=pause):
        pressing = asyncio.create_task(service.press_mouse(session, 1.0, 2.0))
        await asyncio.wait_for(reached.wait(), 5)
        pressing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pressing, 5)

    assert released == ["mouse up"]


# What a press's tests give the mouse-up to answer in: the bound a release has, shortened.
RELEASE_BOUND_S = 0.2


def _press_session(down: Any, up: Any) -> tuple[BrowserActionService, SimpleNamespace]:
    """A service, and a session whose mouse goes down and up by ``down`` and ``up``."""
    mouse = SimpleNamespace(down=down, up=up)
    session = SimpleNamespace(id="session-7", page=SimpleNamespace(mouse=mouse), mouse_position=(10.0, 10.0))
    return BrowserActionService(SimpleNamespace()), session


async def test_a_press_whose_button_is_not_let_go_in_time_says_it_may_still_be_held() -> None:
    # B1-fix1 item 5 (Codex B1-5, Opus F14): the mouse-up's bound raised a RuntimeError, which a click's caller met as
    # HTTP 500 "Internal error", with nothing to say that the press went down: a retry pressed again. The error is a
    # typed one now, not to be retried, and says the button went down and may still be held.
    service, session = _press_session(AsyncMock(), _never)

    with patch.object(actions, "MOUSE_UP_TIMEOUT_SECONDS", RELEASE_BOUND_S), pytest.raises(BrowserActionError) as ended:
        await asyncio.wait_for(service.press_mouse(session, 1.0, 2.0), 5)

    assert (ended.value.code, ended.value.status_code, ended.value.retryable) == ("mouse_not_released", 504, False)
    assert "may still be held" in ended.value.message, ended.value.message


async def test_a_mouse_up_whose_cancel_is_never_answered_still_ends_at_its_bound() -> None:
    # B1-fix1 item 23 (staged triage F01): the bound was asyncio.wait_for, which cancels mouse.up() at its timeout and
    # then waits for the cancel to finish; Playwright's cancelled call waits for the driver to answer its abort, with
    # no bound, so a driver that never answered held the press past the 5 s. The bound no longer waits for the cancel.
    aborted = asyncio.Event()

    async def up() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await aborted.wait()  # the driver's answer to the abort
            raise

    service, session = _press_session(AsyncMock(), up)
    with patch.object(actions, "MOUSE_UP_TIMEOUT_SECONDS", RELEASE_BOUND_S):
        pressing = asyncio.ensure_future(service.press_mouse(session, 1.0, 2.0))
        try:
            done, _ = await asyncio.wait({pressing}, timeout=3 * RELEASE_BOUND_S)
        finally:
            aborted.set()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(pressing), 5)

    assert done, "the press waited for the cancel of its mouse-up"
    assert isinstance(pressing.exception(), BrowserActionError), pressing.exception()


async def test_a_release_that_fails_another_way_after_a_failed_press_keeps_the_press_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # B1-fix1 item 24 (staged triage F06): only a PlaywrightError or a RuntimeError of the release was logged and let
    # go; any other replaced the press's own error. Any error of the release is logged, and the press's is raised.
    service, session = _press_session(
        AsyncMock(side_effect=PlaywrightError("the button did not go down")), AsyncMock(side_effect=ValueError("up"))
    )

    with caplog.at_level(logging.WARNING, logger=actions.logger.name), pytest.raises(PlaywrightError):
        await service.press_mouse(session, 1.0, 2.0)

    assert any("after a press that failed" in record.getMessage() for record in caplog.records), caplog.records


async def test_a_cancelled_press_whose_release_fails_is_logged_as_cancelled(caplog: pytest.LogCaptureFixture) -> None:
    # B1-fix1 item 5 (Opus F19): a press cancelled in its pause, whose release then failed, was logged as a press "that
    # failed": the cancel was the error the press was handling, and the release saw none of its own.
    reached = asyncio.Event()

    async def stalls(*_args: Any) -> None:
        reached.set()
        await asyncio.Event().wait()

    service, session = _press_session(AsyncMock(), AsyncMock(side_effect=PlaywrightError("Target closed")))
    with (
        patch("app.browser.services.actions.asyncio.sleep", new=stalls),
        caplog.at_level(logging.WARNING, logger=actions.logger.name),
    ):
        pressing = asyncio.create_task(service.press_mouse(session, 1.0, 2.0))
        await asyncio.wait_for(reached.wait(), 5)
        pressing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pressing, 5)

    messages = [record.getMessage() for record in caplog.records]
    assert any("after a press that was cancelled" in message for message in messages), messages


async def test_a_placed_click_gives_its_pointer_question_what_its_budget_has_left() -> None:
    # B1-fix1 item 10 (Opus F18): the whole walk up the frames shared one POINTER_CHECK_TIMEOUT_MS, so a page that
    # answered each call in 150 ms ended a placed click on a target two frames down as a timeout, where 1.18.1
    # pressed. The question is given what the click has left, each of its calls a second at most.
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, locator, _watch, _done = _framed_click(True)
        with reading_presses("click", "read", None):
            await service.click_as_playwright_places_it(session, locator, budget=_Budget(5000))

    given = service._receives_pointer_at.await_args.kwargs["timeout_ms"]
    assert POINTER_CHECK_TIMEOUT_MS < given <= 5000, given


def _slow_frames(depth: int, seconds: float) -> SimpleNamespace:
    """A target ``depth`` frames down whose page answers each call in ``seconds``; the locator that finds it."""

    async def slow(answer: Any) -> Any:
        await asyncio.sleep(seconds)
        return answer

    def node(level: int) -> SimpleNamespace:
        frame = SimpleNamespace(
            parent_frame=object() if level < depth else None,
            frame_element=lambda: slow(node(level + 1)),
        )
        return SimpleNamespace(
            bounding_box=lambda: slow(BOX),
            evaluate=lambda _script, _given: slow(True),
            owner_frame=lambda: slow(frame),
            dispose=lambda: slow(None),
        )

    return SimpleNamespace(
        evaluate=lambda *_args, **_named: slow("framed"), element_handle=lambda **_named: slow(node(0))
    )


async def test_a_pointer_question_on_a_slow_page_that_answers_is_answered() -> None:
    # Kept (Opus's probe): each call in 150 ms, two frames up, within what the click has left: a yes, as in 1.18.1.
    locator = _slow_frames(2, 0.15)

    found = await asyncio.wait_for(
        BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=True, timeout_ms=5000), 10
    )

    assert found is True


async def test_each_call_of_a_pointer_question_has_its_own_bound_within_what_the_click_has_left() -> None:
    # A call that never answers still ends the question at POINTER_CHECK_TIMEOUT_MS, however much the click has left.
    target, _outer, locator = _frames_that_stall("bounding_box")

    with patch.object(actions, "POINTER_CHECK_TIMEOUT_MS", BOUND_MS):
        began = time.monotonic()
        with pytest.raises(PlaywrightTimeoutError):
            await asyncio.wait_for(
                BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=True, timeout_ms=5000), 10
            )
        took = time.monotonic() - began

    assert took < WITHIN_THREE_BOUNDS_S, took
    target.dispose.assert_awaited_once()


async def test_a_types_focus_click_reads_its_targets_box_within_its_bound() -> None:
    # B1-fix1 item 13 (Opus F10): the focus click of a type read its target's box with no timeout, so the look-up
    # waited by Playwright's default and the box asked of the handle with no bound at all: a page that did not answer
    # held the type. It is read within FOCUS_CLICK_TIMEOUT_MS; one not read by then is a click Playwright places.
    service, session, _done = _service(CLOSE)
    service.click_as_playwright_places_it = AsyncMock()
    locator = SimpleNamespace(bounding_box=_never)

    with (
        patch.object(actions, "FOCUS_CLICK_TIMEOUT_MS", BOUND_MS),
        patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()),
    ):
        began = time.monotonic()
        await asyncio.wait_for(service.focus_locator(session, locator), 5)
        took = time.monotonic() - began

    assert took < WITHIN_THREE_BOUNDS_S, took
    service.click_as_playwright_places_it.assert_awaited_once()


def _handle_the_page_makes_late() -> tuple[asyncio.Future[Any], asyncio.Event, Callable[..., Awaitable[Any]], Any]:
    """An element_handle whose page makes the handle whatever its caller does (a cancelled call does not stop the
    page): the future the page answers by, an event set once it is asked, the call, and the handle it makes."""
    made: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    asked = asyncio.Event()
    handle = SimpleNamespace(dispose=AsyncMock())

    async def element_handle(*_args: Any, **_given: Any) -> Any:
        asked.set()
        return await asyncio.shield(made)

    return made, asked, element_handle, handle


async def _until_given_back(handle: Any) -> None:
    for _ in range(200):
        if handle.dispose.await_count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the handle was not given back within 2 s")


@pytest.mark.parametrize("by", ["the pointer question", "the placed click"])
async def test_a_target_handle_that_arrives_once_its_click_was_cancelled_is_given_back(by: str) -> None:
    # B1-fix1 item 15 (Opus F12): the click's own look-ups of its target's handle (the placed click's in a frame,
    # and the pointer question's) were awaited as they are: a cancel while one was unanswered left the handle the
    # page made with nobody to give it back. Each is asked as the click's other handle calls are, and a handle that
    # arrives once it was given up on is given back as it arrives.
    made, asked, element_handle, handle = _handle_the_page_makes_late()
    with reading_presses("click", "read", None), patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        if by == "the pointer question":
            locator = SimpleNamespace(evaluate=AsyncMock(return_value="framed"), element_handle=element_handle)
            clicking = asyncio.ensure_future(
                BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=True, timeout_ms=5000)
            )
        else:
            service, session, locator, _watch, _done = _framed_click(True)
            locator.element_handle = element_handle
            clicking = asyncio.ensure_future(
                service.click_as_playwright_places_it(session, locator, budget=_Budget(5000))
            )
        await asyncio.wait_for(asked.wait(), 5)
        clicking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(clicking, 5)
    made.set_result(handle)

    await _until_given_back(handle)


def _frames_whose_handles_are_never_given_back(depth: int) -> tuple[SimpleNamespace, list[SimpleNamespace]]:
    """A target ``depth`` frames down whose page answers every call but the disposals of its handles; the locator
    that finds it, and every handle the walk up the frames takes."""
    handles: list[SimpleNamespace] = []

    def node(level: int) -> SimpleNamespace:
        frame = SimpleNamespace(
            parent_frame=object() if level < depth else None,
            frame_element=AsyncMock(side_effect=lambda: node(level + 1)),
        )
        handle = SimpleNamespace(
            bounding_box=AsyncMock(return_value=BOX),
            evaluate=AsyncMock(return_value=True),
            owner_frame=AsyncMock(return_value=frame),
            dispose=AsyncMock(side_effect=_never),
        )
        handles.append(handle)
        return handle

    locator = SimpleNamespace(
        evaluate=AsyncMock(return_value="framed"), element_handle=AsyncMock(side_effect=lambda **_: node(0))
    )
    return locator, handles


async def test_the_handles_of_a_pointer_question_are_given_back_together() -> None:
    # B1-fix1 item 12 (Opus F17): each handle the walk up the frames took was given back in turn, each within its
    # bound, so a page that answered the walk and then no disposal held the click a bound per handle past its own
    # (1.89 s for eight frames under 200 ms bounds). They are given back together: about one bound in all.
    locator, handles = _frames_whose_handles_are_never_given_back(3)

    with patch.object(actions, "FIELD_RECHECK_TIMEOUT_MS", BOUND_MS):
        began = time.monotonic()
        found = await _ended_within(
            BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=True, timeout_ms=5000), 5
        )
        took = time.monotonic() - began

    assert found is True
    assert len(handles) == 4
    assert all(handle.dispose.await_count == 1 for handle in handles)
    assert took < 2 * BOUND_MS / 1000, took


# B1-fix1 item 17 (Opus T1): the bound sites and late give-backs of a placed click that no test noticed.


def _a_placed_click_that_stalls(stalls: str) -> tuple[Any, Any, Any, list[str]]:
    """A read click Playwright places whose page never answers at ``stalls``: the target's first box (the place is
    counted from it), its box again once the hover is done, or, in a frame, the frame's size."""
    framed = stalls == "the frame's size"
    if framed:
        service, session, locator, _watch, done = _framed_click(True)
        session.page.locator = lambda _selector: SimpleNamespace(evaluate=AsyncMock(side_effect=_never))
        return service, session, locator, done
    service, session, done = _service(CLOSE)
    locator = _locator(done)
    service._receives_pointer_at = AsyncMock(return_value=True)
    boxes = [BOX] if stalls == "the box again" else []

    async def bounding_box(**_given: Any) -> Any:
        if boxes:
            return boxes.pop()
        return await _never()

    locator.bounding_box = bounding_box
    return service, session, locator, done


@pytest.mark.parametrize("stalls", ["the box", "the box again", "the frame's size"])
async def test_a_placed_click_whose_page_stops_answering_at_a_box_or_a_size_ends_at_its_budget(stalls: str) -> None:
    # A27, A33 and A28: each of these reads is asked within what the click has left. Awaited as it is, a page that
    # did not answer held the click and its session past its budget.
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, locator, done = _a_placed_click_that_stalls(stalls)
        began = time.monotonic()
        with reading_presses("click", "read", None), pytest.raises(PlaywrightTimeoutError):
            await asyncio.wait_for(service.click_as_playwright_places_it(session, locator, budget=_Budget(BOUND_MS)), 5)
        took = time.monotonic() - began
    assert took < WITHIN_THREE_BOUNDS_S, took
    assert "mouse down" not in done


async def test_a_pointer_question_whose_record_is_not_answered_ends_at_its_bound() -> None:
    # A22: the read of where the target's document saw the pointer arrive is asked within the question's bound.
    asked: list[tuple[str, list[Any]]] = []
    outer = _framed_node("the frame", SimpleNamespace(parent_frame=None), asked)
    up = SimpleNamespace(parent_frame=object(), frame_element=AsyncMock(return_value=outer))
    target = _framed_node("the target", up, asked)
    answer = target.evaluate

    async def evaluate(script: str, given: Any) -> Any:
        if script != _RECEIVES_POINTER_SCRIPT:
            await _never()
        return await answer(script, given)

    target.evaluate = evaluate
    locator = SimpleNamespace(evaluate=AsyncMock(), element_handle=AsyncMock(return_value=target))
    asking = BrowserActionService._receives_pointer_at(
        locator, 5.0, 6.0, placed=True, watch=SimpleNamespace(), timeout_ms=BOUND_MS
    )

    began = time.monotonic()
    with pytest.raises(PlaywrightTimeoutError):
        await asyncio.wait_for(asking, 5)
    took = time.monotonic() - began

    assert took < WITHIN_THREE_BOUNDS_S, took
    assert asked == []
    target.dispose.assert_awaited_once()


async def test_a_frame_element_that_arrives_once_its_question_ended_is_given_back() -> None:
    # A21: the frame's element a walk up the frames asks for is the page's to keep until it is given back, and one
    # that arrives once the question gave up on it is given back as it arrives.
    made, asked_for, frame_element, handle = _handle_the_page_makes_late()
    asked: list[tuple[str, list[Any]]] = []
    up = SimpleNamespace(parent_frame=object(), frame_element=frame_element)
    target = _framed_node("the target", up, asked)
    locator = SimpleNamespace(evaluate=AsyncMock(return_value="framed"), element_handle=AsyncMock(return_value=target))

    with pytest.raises(PlaywrightTimeoutError):
        await asyncio.wait_for(
            BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=True, timeout_ms=BOUND_MS), 5
        )
    assert asked_for.is_set()
    made.set_result(handle)

    await _until_given_back(handle)


async def test_a_watch_that_arrives_once_its_click_ended_is_stopped_and_given_back() -> None:
    # A16: the pointer watch set up on a framed target is a handle with listeners on the frame's window: one that
    # arrives once the click gave up on it is stopped and given back as it arrives.
    made, asked_for, evaluate_handle, _unused = _handle_the_page_makes_late()
    watch = SimpleNamespace(evaluate=AsyncMock(), dispose=AsyncMock())
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, locator, _watch, _done = _framed_click(True)
        looked_up = locator.element_handle

        async def element_handle(**given: Any) -> Any:
            target = await looked_up(**given)
            target.evaluate_handle = evaluate_handle
            return target

        locator.element_handle = element_handle
        with reading_presses("click", "read", None), pytest.raises(PlaywrightTimeoutError):
            await asyncio.wait_for(service.click_as_playwright_places_it(session, locator, budget=_Budget(BOUND_MS)), 5)
    assert asked_for.is_set()
    made.set_result(watch)

    await _until_given_back(watch)
    watch.evaluate.assert_awaited_once()


def _frames_whose_last_fetch_fails(fails: str, asked: list[Any]) -> SimpleNamespace:
    """A target deeper than POINTER_CHECK_FRAMES frames, every call of whose walk up answers but the frame element
    fetched on its last turn, which raises (``fails`` "raises") or never answers ("stalls"); the target's handle."""

    def node(level: int) -> SimpleNamespace:
        if level == POINTER_CHECK_FRAMES - 1:
            failing = PlaywrightError("the frame has gone") if fails == "raises" else _never
            fetch = AsyncMock(side_effect=failing)
        else:
            fetch = AsyncMock(side_effect=lambda: node(level + 1))
        return _framed_node(f"frame {level}", SimpleNamespace(parent_frame=object(), frame_element=fetch), asked)

    return node(0)


@pytest.mark.parametrize("fails", ["raises", "stalls"])
async def test_a_walk_that_runs_out_of_frames_fetches_no_frame_element_it_would_not_ask(fails: str) -> None:
    # B1-fix1 item 25 (staged F02): on its last turn the walk up the frames fetched the next frame's element, and
    # never asked it: that fetch failing or not answering failed the click. A target POINTER_CHECK_FRAMES frames down
    # or deeper is a yes where the click was placed, by what the documents it asked said.
    asked: list[tuple[str, list[Any]]] = []
    target = _frames_whose_last_fetch_fails(fails, asked)
    locator = SimpleNamespace(evaluate=AsyncMock(return_value="framed"), element_handle=AsyncMock(return_value=target))

    found = await asyncio.wait_for(
        BrowserActionService._receives_pointer_at(locator, 5.0, 6.0, placed=True, timeout_ms=BOUND_MS), 5
    )

    assert found is True
    assert len(asked) == POINTER_CHECK_FRAMES


async def test_a_human_like_clicks_pointer_check_leaves_its_fallback_the_rest_of_the_budget() -> None:
    # B1-fix2 item 4 (Codex round 2 #4): B1-fix1 item 10 gave the check before a human-like press what the click had
    # left. On a page that answered each call of the walk up the frames just inside its bound, the check spent the
    # click's budget, and the click Playwright places, which its no hands the click to, had none left (Codex: 1 ms of
    # 15 s). The check is bounded at POINTER_CHECK_TIMEOUT_MS in all again; the placed click's own keeps what is left.
    service, session, _done = _service(CLOSE)
    service._checks_of_a_click = AsyncMock()
    service.move_mouse_human_like = AsyncMock()
    service.pause_before_press = AsyncMock()
    left_to_the_fallback: list[float] = []

    async def placed(_session: Any, _locator: Any, *, budget: _Budget) -> None:
        left_to_the_fallback.append(budget.left_ms())

    service.click_as_playwright_places_it = placed
    budget_ms = 20 * BOUND_MS
    locator = _slow_frames(POINTER_CHECK_FRAMES, 0.75 * BOUND_MS / 1000)

    with patch.object(actions, "POINTER_CHECK_TIMEOUT_MS", BOUND_MS):
        await asyncio.wait_for(
            service.click_locator_human_like(session, locator, (5.0, 6.0), budget=_Budget(budget_ms)), 10
        )

    assert left_to_the_fallback and left_to_the_fallback[0] > budget_ms / 2, left_to_the_fallback
