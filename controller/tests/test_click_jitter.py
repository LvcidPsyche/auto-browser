"""A click by coordinates is pressed on the element at the point it names, jitter or not.

click_human_like presses up to JITTER_PX (2.5) away from its point each way, so that no two clicks land alike.
Nothing checked what was under the moved point: a click aimed at a small control pressed its neighbour, which the
fast profile's classifier (app/browser/click_target.py) had never read, since it reads the element at the point the
click names. The jittered point is now kept only while the same element is under it; otherwise, or when that
cannot be told, the press is at the point itself. These pin the fallback; test_click_target_chromium.py runs
many clicks at a 3 px control inside a "Delete account" button.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.action_errors import PAGE_NOT_RESPONDING, BrowserActionError
from app.browser.click_target import SAME_ELEMENT_SCRIPT, SAME_ELEMENT_TIMEOUT_MS, same_element_at
from app.browser.services.actions import BrowserActionService
from app.browser.services.element_actions import JITTER_PX

AIMED = (200.0, 100.0)


def _service(same: Any) -> tuple[BrowserActionService, SimpleNamespace, list[tuple[str, float, float]]]:
    """An action service whose comparison of the two points answers ``same`` (or raises it), and what the mouse did."""
    done: list[tuple[str, float, float]] = []
    position = [10.0, 10.0]

    async def move(x: float, y: float) -> None:
        position[:] = [x, y]

    async def down() -> None:
        done.append(("down", *position))

    async def up() -> None:
        done.append(("up", *position))

    evaluate = AsyncMock(side_effect=same) if isinstance(same, BaseException) else AsyncMock(return_value=same)
    manager = SimpleNamespace(
        settings=SimpleNamespace(default_viewport_width=1280, default_viewport_height=720),
        element_ids=SimpleNamespace(evaluate=evaluate),
    )
    mouse = SimpleNamespace(move=move, down=down, up=up)
    session = SimpleNamespace(id="s1", page=SimpleNamespace(mouse=mouse), mouse_position=(10.0, 10.0))
    return BrowserActionService(manager), session, done


async def _click(service: BrowserActionService, session: SimpleNamespace) -> None:
    with patch("app.browser.services.actions.asyncio.sleep", new=AsyncMock()):
        await service.click_human_like(session, *AIMED)


async def test_the_jittered_point_is_pressed_while_the_same_element_is_under_it() -> None:
    service, session, done = _service(True)

    await _click(service, session)

    (down, x, y), (up, *released_at) = done
    assert (down, up) == ("down", "up") and released_at == [x, y]
    assert (x, y) != AIMED and abs(x - AIMED[0]) <= JITTER_PX and abs(y - AIMED[1]) <= JITTER_PX
    assert session.mouse_position == (x, y)
    # Asked of the isolated world, with both points, bounded, and without taking the page's session on a timeout.
    script, arg = service.manager.element_ids.evaluate.await_args.args[1:3]
    assert script == SAME_ELEMENT_SCRIPT
    assert arg == {"x": AIMED[0], "y": AIMED[1], "otherX": x, "otherY": y}
    assert service.manager.element_ids.evaluate.await_args.kwargs == {
        "timeout_ms": SAME_ELEMENT_TIMEOUT_MS,
        "setup_timeout_ms": SAME_ELEMENT_TIMEOUT_MS,
        "keep_session": True,
    }


@pytest.mark.parametrize(
    "same",
    [
        pytest.param(False, id="another element is under the jittered point"),
        pytest.param(None, id="the page gave no answer worth the name"),
        pytest.param("true", id="an answer that is not the script's"),
        pytest.param(BrowserActionError("no answer", code=PAGE_NOT_RESPONDING), id="the page does not answer"),
        pytest.param(RuntimeError("the session is gone"), id="the comparison raises"),
    ],
)
async def test_the_point_itself_is_pressed_when_the_two_differ_or_cannot_be_compared(same: Any) -> None:
    service, session, done = _service(same)

    await _click(service, session)

    assert done == [("down", *AIMED), ("up", *AIMED)]
    assert session.mouse_position == AIMED


async def test_same_element_at_is_true_only_for_the_scripts_own_true() -> None:
    page = object()
    for answer, expected in ((True, True), (False, False), (None, False), (1, False), ({"same": True}, False)):
        element_ids = SimpleNamespace(evaluate=AsyncMock(return_value=answer))
        assert await same_element_at(element_ids, page, (1.0, 2.0), (3.0, 4.0)) is expected
