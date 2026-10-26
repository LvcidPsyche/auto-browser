"""Pointer and keyboard input for an element named by element_id, guarded by checks on that element.

The pointer still moves along the human-like path, to a point inside the
element's visible part, and the 30-120 ms pause still comes before the press,
but the hit test runs after the pause, on the registered element in the
isolated world, right before the mouse goes down. Anything outside the element
at the pointer (a cover, a label for it, an embedded frame) is a refusal, not a
click: there is no fallback to a Playwright click.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any

from ...action_errors import BrowserActionError
from ..element_target import ACTIONABILITY_REFUSALS, ElementTarget, centre

if TYPE_CHECKING:
    from ...browser_manager import BrowserSession

# How long a click that only moves focus waits for its target to take the
# pointer before focusing it programmatically instead.
FOCUS_CLICK_TIMEOUT_MS = 1000
# How far from the target point the pointer lands, at most, in CSS pixels.
JITTER_PX = 2.5
# How far inside the edge of the element's pointer rect a jittered point stays.
_EDGE_MARGIN_PX = 0.5


def _jittered(rect: dict[str, float]) -> tuple[float, float]:
    """A point near the centre of ``rect`` and inside it, however small the visible part is."""
    x, y = centre(rect)
    reach_x = min(JITTER_PX, max(0.0, rect["width"] / 2 - _EDGE_MARGIN_PX))
    reach_y = min(JITTER_PX, max(0.0, rect["height"] / 2 - _EDGE_MARGIN_PX))
    return x + random.uniform(-reach_x, reach_x), y + random.uniform(-reach_y, reach_y)


class ElementActions:
    """Click, hover and focus on an ElementTarget, using the action service's pointer and keyboard pacing."""

    def __init__(self, actions: Any) -> None:
        self._actions = actions

    async def click(self, session: "BrowserSession", element: ElementTarget) -> tuple[float, float]:
        """Click the element once it is ready and only if it is what the pointer hits; returns its target point."""
        rect = await element.wait_until_actionable(verb="clicked", enabled=True, pointer=True)
        cover = await self._press_on(session, element, rect)
        if cover is not None:
            raise element.refusal("element_covered", "clicked", cover=cover)
        return centre(rect)

    async def hover(self, session: "BrowserSession", element: ElementTarget) -> tuple[float, float]:
        point = centre(await element.wait_until_actionable(verb="hovered", pointer=True))
        await self._actions.move_mouse_human_like(session, *point)
        return point

    async def focus(self, session: "BrowserSession", element: ElementTarget) -> None:
        """Put keyboard focus in the element, by a click where it takes the pointer, and prove it is there."""
        try:
            rect = await element.wait_until_actionable(
                verb="typed", enabled=True, pointer=True, timeout_ms=FOCUS_CLICK_TIMEOUT_MS
            )
        except BrowserActionError as exc:
            if exc.code not in ACTIONABILITY_REFUSALS:
                raise
            rect = None  # covered, disabled or hidden: focus it without clicking anything
        if rect is not None:
            await self._press_on(session, element, rect)
            await asyncio.sleep(0.05 + random.random() * 0.1)
        if await element.has_focus():
            return
        await element.focus()
        await self.require_focus(element)

    @staticmethod
    async def require_focus(element: ElementTarget, *, during: str = "") -> None:
        if await element.has_focus():
            return
        raise BrowserActionError(
            f"Keyboard focus would not stay in element_id {element.element_id}{during}, so nothing was typed.",
            action="type",
            code="focus_lost",
            retryable=True,
        )

    async def _press_on(
        self, session: "BrowserSession", element: ElementTarget, rect: dict[str, float]
    ) -> dict[str, Any] | None:
        """Move into the pointer rect, pause, and press only if nothing covers the element there; returns the cover."""
        x, y = _jittered(rect)
        await self._actions.move_mouse_human_like(session, x, y)
        await self._actions.pause_before_press()
        cover = await element.cover_at(x, y)
        if cover is None:
            await self._actions.press_mouse(session, x, y)
        return cover
