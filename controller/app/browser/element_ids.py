"""Element ids the controller owns, kept where the page cannot read, write or forge them.

Observe gives the agent an ``element_id`` for each interactable element, and an
action names its target by one. Observe used to write the id into the page as a
``data-operator-id`` attribute, and an action found the element again with the
selector ``[data-operator-id="<id>"]``. The page owns its DOM: it could set the
attribute itself (observe kept any id it found), give a hidden "Pay now" the id
of the visible "Cancel", or copy an id once observe had written it, and an
action aimed at one element ran on another. The id also went into that selector
unescaped, so a crafted one could add selectors of its own.

The ids now live in a registry in each page's isolated world
(``element_world.py``); observe no longer writes element-id attributes. Ids are
random (96 bits), checked against the exact form the controller issues before
any lookup, and belong to one document: a new document starts an empty
registry, so an id from an earlier document is refused with an error that says
to observe again. A back/forward-cache restore brings back the same document,
and its ids with it. Actions reach the element through ``ElementTarget``
(``element_target.py``).
"""

from __future__ import annotations

import re
import weakref
from typing import TYPE_CHECKING, Any

from ..action_errors import BrowserActionError
from .element_target import ElementTarget
from .element_world import READ, PageWorld, observation_declaration

if TYPE_CHECKING:
    from playwright.async_api import Page

# The form the registry issues; nothing else is ever looked up.
_ELEMENT_ID = re.compile(r"op-[0-9a-f]{24}")
# The selector_hint observe reports beside each element_id.
_SELECTOR_HINT = re.compile(r'\[data-operator-id="([^"]*)"\]')


def selector_hint(element_id: str) -> str:
    return f'[data-operator-id="{element_id}"]'


def hinted_element_id(selector: str | None) -> str | None:
    """The id inside ``selector`` when it is exactly a selector_hint, unvalidated; otherwise None."""
    hint = _SELECTOR_HINT.fullmatch(selector) if selector else None
    return hint.group(1) if hint else None


def validate_element_id(element_id: object) -> str:
    """``element_id`` if it has the exact form observe issues; refused otherwise, before any lookup."""
    if isinstance(element_id, str) and _ELEMENT_ID.fullmatch(element_id):
        return element_id
    raise BrowserActionError(
        f"element_id {str(element_id)[:80]!r} is not one browser.observe issued (those are op- and 24 "
        "lowercase hex digits). Call browser.observe and use an element_id from its interactables.",
        code="invalid_element_id",
        retryable=False,
    )


def target_element_id(*, selector: str | None, element_id: str | None) -> str | None:
    """The element_id a call names, validated: its element_id, or a selector_hint passed as its selector.

    No element carries the hint's attribute any more, so a hint used as a
    selector is read as the id it names rather than matched against the page,
    where only an attribute the page wrote itself could match it.
    """
    if not element_id:
        hinted = hinted_element_id(selector)
        if hinted is not None:
            return validate_element_id(hinted)
    return validate_element_id(element_id) if element_id else None


def refuse_selector_hint(selector: str | None, *, action: str) -> None:
    """Refuse a selector_hint given to a tool that matches selectors against the page.

    No element carries the attribute a hint names, so matching it could only
    find an element the page tagged itself.
    """
    hinted = hinted_element_id(selector)
    if hinted is None:
        return
    raise BrowserActionError(
        f"{selector[:120]} is the selector_hint of element_id {hinted[:80]}, not a selector: no element "
        "carries that attribute. Pass element_id to the action tools (click, type, hover, select_option, "
        "upload) instead, or a CSS selector of your own here.",
        code="selector_hint_not_a_selector",
        action=action,
        retryable=False,
    )


class ElementIds:
    """The controller's element ids, per page, in each page's isolated world."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._worlds: weakref.WeakKeyDictionary[Any, PageWorld] = weakref.WeakKeyDictionary()

    def _world(self, page: "Page") -> PageWorld:
        world = self._worlds.get(page)
        if world is None:
            world = self._worlds[page] = PageWorld(self._settings)
        return world

    async def evaluate(self, page: "Page", script: str, arg: Any = None) -> Any:
        """Run an observation script, ``(arg, elementIds) => ...``, in the page's isolated world."""
        world = self._world(page)
        result = await world.call(page, observation_declaration(script), arg, effect=READ)
        world.remember(result["issued"], result["tag"])
        return result["value"]

    def element(self, page: "Page", element_id: str) -> ElementTarget:
        """The element ``element_id`` names on ``page``; a malformed id is refused before the page is touched."""
        return ElementTarget(self._world(page), page, validate_element_id(element_id))
