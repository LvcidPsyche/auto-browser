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

import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
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
from app.browser.services.actions import (
    _PRESS_PLACE_SCRIPT,
    _RECEIVES_POINTER_SCRIPT,
    ENABLED_POLL_SECONDS,
    PLACE_RECOUNTS,
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
