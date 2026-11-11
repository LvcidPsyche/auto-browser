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
from app.browser.services.actions import _PRESS_PLACE_SCRIPT, ENABLED_POLL_SECONDS, BrowserActionService

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
PLACE = {"x": 12, "y": 7, "border": [3, 2], "boxless": False, "contents": False}
PLACED_AT = (20 + 3 + 12.0, 30 + 2 + 7.0)


def _locator(
    done: list[str],
    *,
    box: Any = BOX,
    place: dict[str, Any] = PLACE,
    enabled: tuple[bool, ...] = (True,),
    each_step_takes: float = 0.0,
) -> SimpleNamespace:
    """A locator whose steps note what they were given. ``enabled`` is what each look at the target finds, the last
    one for good; ``each_step_takes`` is the time a step holds the click for (the clock is the real one)."""
    budgets: dict[str, Any] = {}
    looks = list(enabled)

    def given_budget(step: str, given: dict[str, Any]) -> None:
        budgets.setdefault(step, given.get("timeout"))
        time.sleep(each_step_takes)

    async def is_enabled(**given: Any) -> bool:
        given_budget("enabled", given)
        done.append("is it enabled")
        return looks.pop(0) if len(looks) > 1 else looks[0]

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
        assert script == _PRESS_PLACE_SCRIPT
        given_budget("place", given)
        return place

    async def bounding_box(**given: Any) -> Any:
        given_budget("box", given)
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
            await service.click_as_playwright_places_it(session, _locator(done), timeout_ms=1000)
        assert done == [*CHECKS, placed, "mouse down", "mouse up"]
        session.page.mouse.move.assert_awaited_once_with(*PLACED_AT)
        arg = service.manager.element_ids.evaluate.await_args.args[2]
        assert (arg["x"], arg["y"]) == PLACED_AT

        service, session, done = _service(DELETE)
        with reading_presses("click", "read", None), pytest.raises(PressNeedsApproval):
            await service.click_as_playwright_places_it(session, _locator(done))
        assert done == [*CHECKS, placed]

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


async def test_every_step_of_a_placed_click_takes_the_clicks_own_budget() -> None:
    # The read of the place and the read of the box had no bound of their own: under a short budget (a focus
    # click's) a target that went away between two steps waited out Playwright's default instead.
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        service, session, done = _service(CLOSE)
        locator = _locator(done)
        with reading_presses("click", "read", None):
            await service.click_as_playwright_places_it(session, locator, timeout_ms=250)
    budgets = locator.budgets
    assert list(budgets) == ["enabled", "trial hover", "place", "hover", "box"]
    # The checks share one budget: the trial hover is given what the look at the target left of it.
    assert 200 < budgets["trial hover"] <= budgets["enabled"] <= 250, budgets
    assert (budgets["place"], budgets["hover"], budgets["box"]) == (250, 250, 250)


async def test_the_checks_before_a_human_like_press_are_no_click_either() -> None:
    # The first step of every selector click: it was a trial click too.
    service, session, done = _service(CLOSE)
    locator = _locator(done)
    service.move_mouse_human_like = AsyncMock()
    service.pause_before_press = AsyncMock()
    service._receives_pointer_at = AsyncMock(return_value=True)
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        await service.click_locator_human_like(session, locator, (40.0, 50.0), timeout_ms=250)
    assert done == [*CHECKS, "mouse down", "mouse up"]
    assert set(locator.budgets) == {"enabled", "trial hover"}
    assert all(200 < budget <= 250 for budget in locator.budgets.values()), locator.budgets


async def test_a_disabled_target_is_looked_at_again_until_it_is_enabled() -> None:
    # Each look finds the element the selector names then: the one a page drew in the first one's place too.
    service, session, done = _service(CLOSE)
    locator = _locator(done, enabled=(False, False, True))
    service.move_mouse_human_like = AsyncMock()
    service.pause_before_press = AsyncMock()
    service._receives_pointer_at = AsyncMock(return_value=True)
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()) as waited:
        await service.click_locator_human_like(session, locator, (40.0, 50.0), timeout_ms=250)
    assert done == ["is it enabled", "is it enabled", "is it enabled", "trial hover", "mouse down", "mouse up"]
    assert [call.args for call in waited.await_args_list[:2]] == [(ENABLED_POLL_SECONDS,)] * 2


async def test_a_target_that_stays_disabled_ends_the_click_when_its_budget_does() -> None:
    service, session, done = _service(CLOSE)
    locator = _locator(done, enabled=(False,), each_step_takes=0.02)
    with (
        patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()),
        pytest.raises(PlaywrightTimeoutError, match="enabled"),
    ):
        await service.click_locator_human_like(session, locator, (40.0, 50.0), timeout_ms=50)
    # 20 ms a look, or more, under a budget of 50: looked at more than once, and nothing else was done.
    assert len(done) >= 2 and set(done) == {"is it enabled"}, done


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
