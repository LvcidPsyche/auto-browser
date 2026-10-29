"""browser.drag_drop: from one end to the other, each a point, a CSS selector or a selector_hint.

An end named by a selector_hint is the element observe described, found through
the page's isolated world once it is visible and still (and, for the source,
uncovered); no element carries the hint's attribute, so matching it as CSS
could only find one the page tagged itself. Bringing such an end into view can
scroll the page, or a scrolling part of it, so every other end is read after
all of them are in place: an element end again, without scrolling; a CSS end
for the first time; and a point is refused once anything has scrolled, since it
no longer names what it did. A CSS end with no box is refused, never dragged
from or to the corner. A drag with no element end works as before otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from playwright.async_api import Error as PlaywrightError

from ..action_errors import BrowserActionError
from ..browser import page_read
from ..browser.element_ids import target_element_id
from ..browser.element_target import ElementTarget, centre
from ..browser.page_read import bounded, locked_read

if TYPE_CHECKING:
    from ..tool_inputs import DragDropInput

logger = logging.getLogger(__name__)

# An end: the registered element, a CSS selector, or a point in the viewport.
DragEnd = ElementTarget | str | tuple[float, float]

# The page's scroll offsets and viewport, read in the isolated world.
_VIEW_SCRIPT = "() => ({ x: scrollX, y: scrollY, width: innerWidth, height: innerHeight })"
# The boxes an element is drawn inside, up the flat tree: the slot it is
# assigned to, and a shadow root's host, as well as its parent. Bringing an
# element into view scrolls these, a web component's shadow scroller included,
# whose scroll events stop at its shadow root and never reach the document. A
# slot in a closed shadow root is not visible from outside it. A fragment that
# is not a shadow root (a template's content) ends the walk. Read through the
# prototypes' own getters: a form's named controls shadow its properties, in
# the world too. Defines `drawnIn(registry, ids)`, the boxes of every end, `offsets(boxes)` and `same(a, b)`.
_BOXES_JS = """const get = (proto, name) => Object.getOwnPropertyDescriptor(proto, name).get;
  const nodeType = get(Node.prototype, 'nodeType');
  const parentNode = get(Node.prototype, 'parentNode');
  const assignedSlot = get(Element.prototype, 'assignedSlot');
  const host = get(ShadowRoot.prototype, 'host');
  const scrollLeft = get(Element.prototype, 'scrollLeft');
  const scrollTop = get(Element.prototype, 'scrollTop');
  const boxes = (el) => {
    const found = [];
    for (let node = el; node; ) {
      const type = nodeType.call(node);
      if (type === 1) found.push(node);
      if (type === 11) node = node instanceof ShadowRoot ? host.call(node) : null;
      else node = (type === 1 && assignedSlot.call(node)) || parentNode.call(node);
    }
    return found;
  };
  const offsets = (found) => found.map((box) => [scrollLeft.call(box), scrollTop.call(box)]);
  const same = (a, b) => a.length === b.length && a.every((item, i) => item === b[i]);
  const drawnIn = (registry, ids) => {
    const found = [];
    for (const id of ids) {
      const el = registry.element(id);
      if (el) found.push(...boxes(el));
    }
    return found;
  };"""
# Starts watching for a scroll while the element ends are brought into view:
# the window's, any scrolling element's in the document (a list scrolled inside
# the page leaves the window where it was), and any box the ends are drawn
# inside. Scroll events reach a capture listener on the document from every
# element outside shadow roots; this one is the world's, which the page cannot
# remove. A page can stop the events before they reach it, so the boxes'
# offsets are kept too. A watch is a read, which the world sends again when its
# answer is lost: a second start under the same nonce answers as the first did
# and keeps what it has seen. Answers the window's offsets.
_WATCH_SCRIPT = (
    """({ nonce, ids }, registry) => {
  """
    + _BOXES_JS
    + """
  const previous = registry.scrollWatches.get(nonce);
  if (previous) return previous.start;
  const watch = { scrolled: false, listener: null, ids, boxes: [], offsets: [], start: { x: scrollX, y: scrollY } };
  watch.listener = () => { watch.scrolled = true; };
  watch.boxes = drawnIn(registry, ids);
  watch.offsets = offsets(watch.boxes);
  document.addEventListener('scroll', watch.listener, { capture: true, passive: true });
  registry.scrollWatches.set(nonce, watch);
  return watch.start;
}"""
)
# Ends the watch: whether anything scrolled (a watch gone with its document counts), and the window's offsets.
_UNWATCH_SCRIPT = (
    """(nonce, registry) => {
  """
    + _BOXES_JS
    + """
  const watch = registry.scrollWatches.get(nonce);
  registry.scrollWatches.delete(nonce);
  if (!watch) return { scrolled: true, x: scrollX, y: scrollY };
  document.removeEventListener('scroll', watch.listener, { capture: true });
  // An end drawn inside other boxes now (a component that slotted it into its
  // shadow scroller meanwhile) may have been scrolled where nothing was watched.
  const moved = !same(drawnIn(registry, watch.ids), watch.boxes)
    || offsets(watch.boxes).some(([x, y], i) => x !== watch.offsets[i][0] || y !== watch.offsets[i][1]);
  return { scrolled: watch.scrolled || moved, x: scrollX, y: scrollY };
}"""
)
# How much of a CSS selector a refusal repeats.
_SELECTOR_LIMIT = 100
# How long a drag waits for the mouse button to be released.
MOUSE_UP_TIMEOUT_SECONDS = 5.0
# How long a drag that failed or reached its deadline waits for its scroll watch to be removed: it runs
# after the deadline, which waits for it, and the watch goes with the page anyway. So a drag holds the
# session at most its deadline, this, and the release's bound.
UNWATCH_TIMEOUT_SECONDS = 2.0
_WAY_OUT = (
    "Make the viewport tall enough for both ends (browser.set_viewport), or scroll so both are in view, "
    "then drag again."
)


@dataclass
class _Button:
    """Whether the drag may have pressed the mouse button: set before mouse.down() is sent."""

    may_be_down: bool = False


async def drag_drop(manager: Any, payload: "DragDropInput") -> dict[str, Any]:
    session = await manager.get_session(payload.session_id)
    button = _Button()
    # Outside the action pipeline, so under the session lock here, checked before and after.
    async with locked_read(manager, session):
        source = _end(manager, session, payload.source_selector, payload.source_x, payload.source_y, "source")
        target = _end(manager, session, payload.target_selector, payload.target_x, payload.target_y, "target")
        deadline = _deadline_seconds(manager, source, target)
        try:
            result = await bounded(_drag(manager, session, payload, source, target, button), seconds=deadline)
        except BaseException:
            # The drag failed or reached its deadline, perhaps with the button down: let it go, or
            # the page keeps it pressed for whatever runs next. The drag's own error is the one reported.
            if button.may_be_down:
                await _release_after_failure(session.page)
            raise
        # After the deadline, not under it: a deadline that expired during the release cancelled it.
        if button.may_be_down:
            await _release_through_a_cancel(session.page)
        return result


def _deadline_seconds(manager: Any, source: DragEnd, target: DragEnd) -> float:
    """The drag's deadline: the page-call bound, and ACTION_TIMEOUT_MS for each end named by element_id or a CSS
    selector, the most it waits for its element (its own refusal comes then); the bound alone could end first."""
    named = sum(not isinstance(end, tuple) for end in (source, target))
    return page_read.PAGE_READ_TIMEOUT_SECONDS + named * manager.settings.action_timeout_ms / 1000


async def _drag(
    manager: Any, session: Any, payload: "DragDropInput", source: DragEnd, target: DragEnd, button: _Button
) -> dict[str, Any]:
    by_id = any(isinstance(end, ElementTarget) for end in (source, target))
    points = [name for end, name in ((source, "source"), (target, "target")) if isinstance(end, tuple)]
    await _bring_element_ends_into_view(manager, session, source, target, points if by_id else [])
    sx, sy = await _point(manager, session, source, checked=by_id)
    tx, ty = await _point(manager, session, target, checked=by_id)

    await session.page.mouse.move(sx, sy)
    if isinstance(source, ElementTarget):
        # As for a click: the source is hit-tested after the pause, right
        # before the button goes down. The target is not: drag libraries put
        # the dragged element under the pointer.
        await manager.actions.pause_before_press()
        cover = await source.cover_at(sx, sy)
        if cover is not None:
            raise source.refusal("element_covered", "dragged", cover=cover)
    # From here the button may be down, also if mouse.down() never answers: drag_drop lets it go.
    button.may_be_down = True
    await session.page.mouse.down()
    await session.page.mouse.move(tx, ty, steps=10)
    return {"session_id": payload.session_id, "from": {"x": sx, "y": sy}, "to": {"x": tx, "y": ty}}


async def _release_after_failure(page: Any) -> None:
    try:
        await _release_through_a_cancel(page)
    except (PlaywrightError, RuntimeError) as exc:
        logger.warning("could not release the mouse button after a drag that failed: %s", exc)


async def _release_through_a_cancel(page: Any) -> None:
    """The release after a drag, whether it succeeded or failed, carried through a cancel that arrives
    meanwhile (a client giving up as the drag ended): the button goes up before the session lock is let go,
    then the cancel goes on. The release has its own bound, so the wait for it ends. A further cancel
    while it is waited for is held too (a server shutdown after a client's), and one is raised at the end."""
    release = asyncio.ensure_future(_release_button(page))
    cancel: asyncio.CancelledError | None = None
    while not release.done():
        try:
            # asyncio.wait does not cancel the release when this task is cancelled, nor raise its error.
            await asyncio.wait([release])
        except asyncio.CancelledError as exc:
            cancel = cancel or exc
    if cancel is None:
        release.result()
        return
    if not release.cancelled() and release.exception() is not None:
        logger.warning("could not release the mouse button after a drag that was cancelled: %s", release.exception())
    raise cancel


async def _release_button(page: Any) -> None:
    """mouse.up(), with its own bound, sent once after the drag and outside its deadline, still under the lock."""
    try:
        await asyncio.wait_for(page.mouse.up(), timeout=MOUSE_UP_TIMEOUT_SECONDS)
    except TimeoutError:
        raise RuntimeError(f"The mouse button was not released within {MOUSE_UP_TIMEOUT_SECONDS:g} seconds.") from None


def _end(manager: Any, session: Any, selector: str | None, x: float | None, y: float | None, name: str) -> DragEnd:
    if not selector:
        if x is None or y is None:
            raise ValueError(f"Provide {name}_selector or {name}_x/{name}_y")
        return x, y
    element_id = target_element_id(selector=selector, element_id=None)
    if element_id is None:
        return selector
    return manager.element_ids.element(session.page, element_id)


async def _bring_element_ends_into_view(
    manager: Any, session: Any, source: DragEnd, target: DragEnd, points: list[str]
) -> None:
    """Wait on every end named by element_id; refuse the drag when that scrolled anything under the ``points``."""
    nonce = secrets.token_hex(16) if points else None
    ids = [end.element_id for end in (source, target) if isinstance(end, ElementTarget)]
    before = None
    try:
        # Inside the try: a call cancelled after the watch started still removes it.
        if nonce:
            before = await manager.element_ids.evaluate(session.page, _WATCH_SCRIPT, {"nonce": nonce, "ids": ids})
        for end, name in ((source, "source"), (target, "target")):
            if isinstance(end, ElementTarget):
                await end.wait_until_actionable(verb="dragged", pointer=name == "source")
    except BaseException:
        if nonce:
            await _stop_watching_quietly(manager, session, nonce)
        raise
    if nonce is None:
        return
    after = await manager.element_ids.evaluate(session.page, _UNWATCH_SCRIPT, nonce)
    # The window's offsets as well as the listener: a page can stop scroll events before they reach it.
    if after["scrolled"] or _scrolled(before, after):
        raise BrowserActionError(
            f"Bringing the end named by element_id into view scrolled the page, or a scrolling part of it, so "
            f"the {' and '.join(points)} coordinates no longer point where they did, and nothing was dragged. "
            f"{_WAY_OUT}",
            code="drag_page_scrolled",
            retryable=False,
        )


async def _stop_watching_quietly(manager: Any, session: Any, nonce: str) -> None:
    try:
        await asyncio.wait_for(
            manager.element_ids.evaluate(session.page, _UNWATCH_SCRIPT, nonce), timeout=UNWATCH_TIMEOUT_SECONDS
        )
    except (PlaywrightError, BrowserActionError, TimeoutError) as exc:
        # The wait's own error is the one to report; the listener goes with the page.
        logger.debug("drag: the scroll watch could not be removed: %s", exc)


async def _point(manager: Any, session: Any, end: DragEnd, *, checked: bool) -> tuple[float, float]:
    """Where the pointer goes for one end, read now; ``checked`` refuses a CSS end outside the viewport."""
    if isinstance(end, tuple):
        return end
    if isinstance(end, ElementTarget):
        rect = await end.pointer_rect()
        if rect is None:
            raise _not_in_view(f"element_id {end.element_id}")
        return centre(rect)
    box = await session.page.locator(end).first.bounding_box()
    if not box:
        raise BrowserActionError(
            f"The element matching {end[:_SELECTOR_LIMIT]!r} has no box on the page (it is hidden or not "
            "rendered), so nothing was dragged. Drag from and to elements that are shown, or by coordinates.",
            code="drag_end_not_visible",
            retryable=False,
        )
    point = centre(box)
    if checked and not _inside(point, await _view(manager, session)):
        raise _not_in_view(f"The element matching {end[:_SELECTOR_LIMIT]!r}")
    return point


async def _view(manager: Any, session: Any) -> dict[str, float]:
    return await manager.element_ids.evaluate(session.page, _VIEW_SCRIPT)


def _scrolled(before: dict[str, float], after: dict[str, float]) -> bool:
    return before["x"] != after["x"] or before["y"] != after["y"]


def _inside(point: tuple[float, float], view: dict[str, float]) -> bool:
    return 0 <= point[0] < view["width"] and 0 <= point[1] < view["height"]


def _not_in_view(what: str) -> BrowserActionError:
    return BrowserActionError(
        f"{what} is no longer in the viewport once both ends of the drag are in place: bringing the other end "
        f"into view scrolled it away, so nothing was dragged. {_WAY_OUT}",
        code="drag_ends_not_in_view",
        retryable=False,
    )
