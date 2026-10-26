"""What an action does to the element an element_id names, done on the element itself in the isolated world.

The first fix put a one-time marker attribute on the element and let a
Playwright locator for it run Playwright's checks and some of the actions. The
page could see the marker arrive and leave, move it, or move the element, so it
still chose what Playwright checked and acted on (a click on "Cancel" paid,
through a hidden label for Cancel and a marker moved to a decoy).

Click, hover, type and select now never ask the page where the element is. The
registered element is scrolled into view and waited on in the world until it is
visible, stable and, where it matters, enabled (by Playwright's rules, ported);
the pointer goes to the centre of its first client rect inside the viewport (as
Playwright's clickable point does); a cover makes the wait scroll again with
another alignment; the hit test right before the mouse is pressed accepts only
the element or what is inside it (no label for it, and no embedded frame, except
one behind a closed shadow root, which the hit test cannot see into);
focus is checked on the element before keys go out and again around a clear; a
selection is made on the element, scrolled into view, by Playwright's own
matching rules. None of these writes anything into the page.

Uploading still needs Playwright's ``set_input_files`` (it carries the file to
a remote browser), so the element is marked for that one action, resolved once
to a handle, and checked to be the only marked element and the registered one;
the marker is then removed wherever it is. Once the file has gone in, nothing
is raised: whether the registered input holds it is recorded as confirmed or
not_confirmed, best-effort, since a page can forge a file of the same name and
size, or empty the field as it reads it.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from playwright.async_api import Error as PlaywrightError

from ..action_errors import PAGE_NOT_RESPONDING, BrowserActionError
from .element_scripts import (
    ELEMENT_ACTIONABLE_SCRIPT,
    ELEMENT_COVER_SCRIPT,
    ELEMENT_FOCUS_SCRIPT,
    ELEMENT_HAS_FOCUS_SCRIPT,
    ELEMENT_POINTER_RECT_SCRIPT,
    ELEMENT_SELECT_SCRIPT,
    MARKER_ATTRIBUTE,
    UPLOAD_LANDED_SCRIPT,
    UPLOAD_MARK_SCRIPT,
    UPLOAD_MARKED_ALONE_SCRIPT,
    UPLOAD_UNMARK_SCRIPT,
)
from .element_world import CHANGES_PAGE, IDEMPOTENT, READ, PageWorld, element_declaration

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

# How much longer than its own wait a script that waits in the page may take to answer.
_ANSWER_MARGIN_MS = 2000

# What each refusal says about the element, after "element_id <id> ".
_REFUSALS = {
    "element_not_visible": "is not visible (it has no box, or display or visibility hides it)",
    "element_disabled": "is disabled",
    "element_not_stable": "kept moving",
    "element_covered": "is covered at the pointer by an element outside it",
    "element_not_in_view": "has no part inside the viewport, even scrolled into view",
    "element_not_a_select": "is not a <select> or a label for one",
    "option_not_found": "has no option matching the one asked for",
    "option_disabled": "has the option asked for, but it is disabled",
    "element_not_a_file_input": 'is not an <input type="file"> or a label for one',
}
# How much of a refusal's description of a cover, and of its advice, page-chosen text can fill.
_COVER_TEXT_LIMIT = 200
# The refusals that say the element is not ready for a pointer, as opposed to gone.
ACTIONABILITY_REFUSALS = frozenset(
    {"element_not_visible", "element_disabled", "element_not_stable", "element_covered", "element_not_in_view"}
)


def centre(rect: dict[str, float]) -> tuple[float, float]:
    return (float(rect["x"] + rect["width"] / 2), float(rect["y"] + rect["height"] / 2))


def _covered_by(cover: dict[str, Any]) -> tuple[str, str]:
    """What the refusal says covers the element, after "element_id <id> ", and what the caller can do instead."""
    if not cover.get("tag"):
        return "is not under the pointer: no element of the page is there", ""
    name = f"<{cover['tag']}"
    if cover.get("id"):
        name += f' id="{cover["id"]}"'
    elif cover.get("cls"):
        name += f' class="{cover["cls"]}"'
    name += ">"
    if cover.get("frame"):
        return (
            f"is covered at the pointer by an embedded frame, {name}, whose content observe never describes",
            " To reach the element itself, click a part of it outside the frame by coordinates.",
        )
    if not cover.get("label"):
        return f"is covered at the pointer by {name}, outside it", ""
    if cover.get("inLabel"):
        what = f"is covered at the pointer by {name} inside a <label> for it"
    else:
        what = f"is covered at the pointer by a {name} for it"
    advice = " A click by element_id never lands on a label: to click the label, use {}."
    if cover.get("labelSelector"):
        # Given whole or not at all: the bound on the advice must not cut the selector short.
        with_selector = advice.format(f"the selector {cover['labelSelector']} or its coordinates")
        if len(with_selector) <= _COVER_TEXT_LIMIT:
            return what, with_selector
    return what, advice.format("its coordinates")


class ElementTarget:
    """The element an element_id names on one page, reached only through the isolated world.

    Each call looks the id up again. The first answer fixes the document: a
    later call in the same action that finds a new document is refused.
    """

    def __init__(self, world: PageWorld, page: "Page", element_id: str) -> None:
        self._world = world
        self._page = page
        self.element_id = element_id
        self._tag: str | None = None

    async def evaluate(
        self, script: str, arg: Any = None, *, effect: str, attached: bool = True, wait_ms: int = 0
    ) -> Any:
        """Run ``(el, arg, elementIds) => ...`` on the element in the world; ``effect`` as for ``PageWorld.call``."""
        reply = await self._world.call(
            self._page,
            element_declaration(script, attached=attached),
            self.element_id,
            self._tag,
            arg,
            timeout_ms=self._world.timeout_ms + wait_ms + (_ANSWER_MARGIN_MS if wait_ms else 0),
            effect=effect,
        )
        if reply["status"] != "ok":
            raise self._world.lookup_error(self.element_id, reply["status"], reply["tag"])
        self._tag = reply["tag"]
        return reply.get("value")

    def refusal(
        self,
        code: str,
        verb: str,
        *,
        waited_ms: int | None = None,
        cover: dict[str, Any] | None = None,
        scrolled: bool = False,
    ) -> BrowserActionError:
        waited = f" after waiting {waited_ms} ms" if waited_ms else ""
        what, instead = (_REFUSALS[code], "") if cover is None else _covered_by(cover)
        what, instead = what[:_COVER_TEXT_LIMIT], instead[:_COVER_TEXT_LIMIT]
        moved = "; the page was scrolled to try other alignments" if scrolled else ""
        return BrowserActionError(
            f"element_id {self.element_id} {what}{waited}, so nothing was {verb}{moved}.{instead} "
            "Observe again, and act on the element once it is ready or on another one.",
            code=code,
            retryable=True,
        )

    def _raise_refused(self, result: dict[str, Any], verb: str, waited_ms: int) -> None:
        if result["refused"] == "element_gone":
            raise self._world.lookup_error(self.element_id, "gone", self._tag or "")
        raise self.refusal(
            result["refused"],
            verb,
            # A cover that never moves is refused before the deadline.
            waited_ms=None if result.get("early") else waited_ms,
            cover=result.get("cover"),
            scrolled=bool(result.get("scrolled")),
        )

    async def wait_until_actionable(
        self, *, verb: str, enabled: bool = False, pointer: bool = False, timeout_ms: int | None = None
    ) -> dict[str, float]:
        """The element's pointer rect once it is ready (see ELEMENT_ACTIONABLE_SCRIPT); a refusal says why it never was."""
        timeout_ms = self._world.timeout_ms if timeout_ms is None else timeout_ms
        result = await self.evaluate(
            ELEMENT_ACTIONABLE_SCRIPT,
            {"timeoutMs": timeout_ms, "enabled": enabled, "pointer": pointer},
            effect=IDEMPOTENT,  # scrolls the element into view, then reads
            wait_ms=timeout_ms,
        )
        if "refused" in result:
            self._raise_refused(result, verb, timeout_ms)
        return result["rect"]

    async def pointer_rect(self) -> dict[str, float] | None:
        """The element's pointer rect now, without scrolling; None when no part of it is in the viewport."""
        return await self.evaluate(ELEMENT_POINTER_RECT_SCRIPT, effect=READ)

    async def cover_at(self, x: float, y: float) -> dict[str, Any] | None:
        """What covers the element at (x, y), or None when the pointer there hits the element or inside it."""
        return await self.evaluate(ELEMENT_COVER_SCRIPT, [x, y], effect=READ)

    async def focus(self) -> None:
        await self.evaluate(ELEMENT_FOCUS_SCRIPT, effect=CHANGES_PAGE)

    async def has_focus(self) -> bool:
        return bool(await self.evaluate(ELEMENT_HAS_FOCUS_SCRIPT, True, effect=READ))

    async def select_option(
        self, *, value: str | None = None, label: str | None = None, index: int | None = None
    ) -> list[str]:
        """Select by index, else value (which matches a label too, as in Playwright), else label."""
        if index is not None:
            wanted = {"index": index}
        elif value is not None:
            wanted = {"valueOrLabel": value}
        else:
            wanted = {"label": label}
        timeout_ms = self._world.timeout_ms
        result = await self.evaluate(
            ELEMENT_SELECT_SCRIPT,
            {"wanted": [wanted], "timeoutMs": timeout_ms},
            effect=CHANGES_PAGE,
            wait_ms=timeout_ms,
        )
        if "refused" in result:
            self._raise_refused(result, "selected", timeout_ms)
        return result["values"]

    async def set_input_files(self, path: str, *, on_transfer: Callable[[], None]) -> dict[str, str]:
        """Playwright's ``set_input_files`` on this element, bound to it as tightly as a marker allows.

        A page script that moves the marker in the moment between the last
        check and Playwright's resolution can still send the file to another
        file input of the same document. ``on_transfer`` is called as soon as
        Playwright's transfer returns, before anything else is awaited, so a
        caller cancelled during the checks after it knows the file went in.
        Answers the delivery: ``confirmed`` when the registered input holds a
        file of this name and size, else ``not_confirmed`` with the reason. It
        is never an error once the file has gone in, since a retry would upload
        it again.
        """
        nonce = secrets.token_hex(16)
        size = os.path.getsize(path)
        try:
            if not await self.evaluate(UPLOAD_MARK_SCRIPT, nonce, effect=CHANGES_PAGE):
                raise self.refusal("element_not_a_file_input", "uploaded")
            await self._require_marked_alone(nonce)
            handle = await self._resolve_marked(nonce)
            try:
                await self._require_marked_alone(nonce)
                await handle.set_input_files(path)
                on_transfer()
            finally:
                await self._dispose(handle)
            return await self._delivery(nonce, os.path.basename(path), size)
        finally:
            await self._unmark(nonce)

    async def _delivery(self, nonce: str, name: str, size: int) -> dict[str, str]:
        try:
            missing = await self.evaluate(UPLOAD_LANDED_SCRIPT, [nonce, name, size], effect=READ, attached=False)
        except (PlaywrightError, BrowserActionError) as exc:
            # The file went in; only the check could not run (the page navigated, closed or stopped answering).
            logger.info("element %s: upload not confirmed: %s", self.element_id, exc)
            return {"outcome": "not_confirmed", "reason": f"the check could not run: {str(exc).splitlines()[0][:200]}"}
        if missing is None:
            return {"outcome": "confirmed"}
        return {"outcome": "not_confirmed", "reason": missing}

    async def _resolve_marked(self, nonce: str) -> Any:
        try:
            return await self._page.locator(f'[{MARKER_ATTRIBUTE}="{nonce}"]').element_handle(
                timeout=self._world.timeout_ms
            )
        except PlaywrightError as exc:
            # Strict mode found a second marked node (a copy in an open shadow
            # root): the page copied the marker, which a retry would not get past.
            if "strict mode violation" not in exc.message:
                raise
            raise self._marker_moved() from exc

    async def _require_marked_alone(self, nonce: str) -> None:
        if not await self.evaluate(UPLOAD_MARKED_ALONE_SCRIPT, nonce, effect=READ):
            raise self._marker_moved()

    def _marker_moved(self) -> BrowserActionError:
        return BrowserActionError(
            f"The page moved or copied the one-time marker on element_id {self.element_id}, or moved the "
            "element where the document cannot see it, so nothing was uploaded. Observe again before retrying.",
            code="element_target_moved",
            retryable=False,
        )

    async def _dispose(self, handle: Any) -> None:
        try:
            await handle.dispose()
        except PlaywrightError as exc:
            # The page went during the upload; its handles went with it.
            logger.debug("element %s: upload handle not disposed: %s", self.element_id, exc)

    async def _unmark(self, nonce: str) -> None:
        try:
            await self._world.call(self._page, UPLOAD_UNMARK_SCRIPT, self.element_id, nonce, effect=IDEMPOTENT)
        except PlaywrightError as exc:
            # The page went (closed, crashed): the marker went with it.
            logger.debug("element %s: marker not removed: %s", self.element_id, exc)
        except BrowserActionError as exc:
            if exc.code != PAGE_NOT_RESPONDING:
                raise
            logger.warning("element %s: marker not removed, the page stopped answering", self.element_id)
