"""What a fast-profile click lands on, read from the page, not taken from the model's label (STORE-42).

In the fast profile a decision's own ``risk_category`` decides whether its click asks for approval, and
``read`` and ``write`` ask for nothing. That label is written by a model reading the page it acts on, and a page
can steer it to call a click on "Delete account" either one. So before the approval decision, a click so labelled
has its target read: a button whose label starts with a phrase of CLICK_LABEL_PHRASES is recorded with that
phrase's kind, and asks for approval as an honest label does; a form's submit button labelled ``read`` is recorded
as ``write``, which the fast profile does not ask about. A target that cannot be read (none there, a page that
does not answer) changes nothing, and the click reports its own failure.

The target is found as the click will find it. One named by element id or by a point is read in the controller's
isolated world (element_world.py), where no page script runs. One named by a selector is resolved by Playwright, as
the click resolves it (``page.locator(selector).first``: CSS that pierces open shadow roots, ``text=``, ``role=``,
``xpath=``, ``>>`` chains), and described where Playwright evaluates, which is where the page's own script runs.
Reading it as CSS in the isolated world missed every other dialect, and could read another element than the one
clicked (for ``.a, .b`` Playwright's first match may sit in a shadow root that ``document.querySelector`` never
enters). The text that steers a model is content on an honest site, not the site's script; a page whose own script
is hostile can falsify this read, as it can name its button "Continue" or change it after any read.

Not caught: a label the table lacks, a button drawn on a canvas, a link, a label given only by aria-labelledby, an
icon or an image, and a form sent by Enter in one of its fields (a press, not a click).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from ..models import ApprovalKind, BrowserActionDecision, RiskCategory
from .element_ids import target_element_id

if TYPE_CHECKING:
    from playwright.async_api import Page

    from .element_ids import ElementIds

logger = logging.getLogger(__name__)

# The labels the fast profile asks no approval for: a click with one has its target read.
CHECKED_LABELS = frozenset({"read", "write"})
# Deliberately narrow: matched case-insensitively, as whole words, at the start of a button's label. Bare "Close",
# "Cancel", "Dismiss", "OK", "Continue", "Next" and "Accept" are left out on purpose: they are on nearly every page
# and mostly harmless, and an approval on each would make the fast profile unusable.
CLICK_LABEL_PHRASES: dict[ApprovalKind, tuple[str, ...]] = {
    "payment": (
        "pay",
        "place order",
        "buy now",
        "confirm payment",
        "confirm purchase",
        "complete purchase",
        "purchase",
    ),
    "destructive": (
        "delete",
        "remove",
        "permanently delete",
        "cancel subscription",
        "close account",
        "deactivate account",
    ),
    "account_change": ("change password", "update password", "reset password", "change email", "update email"),
    "post": ("publish", "post", "send"),
}
# A longer label is not a button's name but a sentence about one ("How to delete your account").
MAX_LABEL_CHARS = 40
# The read's bound, for the message that runs it and, apart, for the messages that set it up: past it the click
# goes on unclassified.
READ_TIMEOUT_MS = 2000
# How much of each label the page hands over: enough for the lines the label rules read.
_LABEL_CUT = MAX_LABEL_CHARS * 4
_BUTTON_INPUT_TYPES = frozenset({"button", "submit", "image", "reset"})
_PHRASE_PATTERNS = {
    kind: re.compile("^(?:" + "|".join(re.escape(phrase) for phrase in phrases) + r")(?!\w)")
    for kind, phrases in CLICK_LABEL_PHRASES.items()
}
_LEADING_SYMBOLS = re.compile(r"^[^\w]+")

# (el, limit) => what a click on el lands on, or null. The element counts as the button, input or link it sits in,
# as a click on it is one on them. Both reads below describe their element with it, so they cannot drift apart.
DESCRIBE_TARGET_SCRIPT = r"""
(el, limit) => {
  if (!el || !el.closest) return null;
  const target = el.closest('button, input, [role="button" i], a[href]') || el;
  const tag = target.localName;
  const type = tag === 'button' || tag === 'input' ? String(target.type || '').toLowerCase() : '';
  const role = (target.getAttribute('role') || '').trim().split(/\s+/)[0].toLowerCase();
  const submits = (tag === 'button' && type === 'submit')
    || (tag === 'input' && (type === 'submit' || type === 'image'));
  const cut = (value) => (typeof value === 'string' ? value.slice(0, limit) : '');
  const labels = [cut(target.getAttribute('aria-label')), cut(target.innerText)];
  if (tag === 'input') labels.push(cut(target.value), cut(target.getAttribute('alt')));
  return { tag, type, role, submits: Boolean(submits && target.form), labels };
}
"""
# Runs in the isolated world: (arg, registry) => what the click would land on, or null. arg names the target as
# the click does when it names no selector: {elementId} or {x, y}.
CLICK_TARGET_SCRIPT = (
    r"""
(arg, registry) => {
  let el = null;
  if (arg.elementId) {
    const found = registry.find(arg.elementId);
    el = found.status === 'ok' ? found.element : null;
  } else {
    el = document.elementFromPoint(arg.x, arg.y);
    for (let depth = 0; el && el.shadowRoot && depth < 8; depth += 1) {
      const inner = el.shadowRoot.elementFromPoint(arg.x, arg.y);
      if (!inner || inner === el) break;
      el = inner;
    }
  }
  return ("""
    + DESCRIBE_TARGET_SCRIPT
    + r""")(el, arg.limit);
}
"""
)


def _label_kind(label: str) -> ApprovalKind | None:
    shown = _LEADING_SYMBOLS.sub("", " ".join(label.split())).casefold()
    if not shown or len(shown) > MAX_LABEL_CHARS:
        return None
    for kind, pattern in _PHRASE_PATTERNS.items():
        if pattern.match(shown):
            return kind
    return None


def classified_risk(target: Any, label: RiskCategory | None) -> RiskCategory | None:
    """What a click labelled ``label`` on ``target`` (CLICK_TARGET_SCRIPT's answer) is recorded as; None keeps it."""
    if not isinstance(target, dict) or label not in CHECKED_LABELS:
        return None
    tag, kind_of_input, role = (str(target.get(name) or "") for name in ("tag", "type", "role"))
    button_like = tag == "button" or (tag == "input" and kind_of_input in _BUTTON_INPUT_TYPES) or role == "button"
    if button_like:
        labels = target.get("labels")
        for text in labels if isinstance(labels, list) else ():
            kind = _label_kind(text) if isinstance(text, str) else None
            if kind is not None:
                return kind
    if target.get("submits") is True and label == "read":
        return "write"
    return None


async def read_click_target(element_ids: ElementIds, page: Page, decision: BrowserActionDecision) -> Any:
    """What the click ``decision`` makes would land on (DESCRIBE_TARGET_SCRIPT's answer), or None, logged at debug,
    when it cannot be read. The target is named as the click names it: an element id, else a selector, else a point."""
    try:
        element_id = target_element_id(selector=decision.selector, element_id=decision.element_id)
        if decision.selector and not element_id:
            return await _read_selected(page, decision.selector)
        arg: dict[str, Any] = {"elementId": element_id} if element_id else {"x": decision.x, "y": decision.y}
        return await element_ids.evaluate(
            page,
            CLICK_TARGET_SCRIPT,
            {**arg, "limit": _LABEL_CUT},
            timeout_ms=READ_TIMEOUT_MS,
            setup_timeout_ms=READ_TIMEOUT_MS,
            # Past its bound only this read is given up: the page's session stays for the calls beside it.
            keep_session=True,
        )
    except Exception as exc:
        logger.debug(
            "the target of a %s click could not be read; it is not classified: %r", decision.risk_category, exc
        )
        return None


async def _read_selected(page: Page, selector: str) -> Any:
    """The element ``selector`` names, found as the click finds it (BrowserActionService.click), and described.

    Playwright waits for the element to be attached, as the click will, but for no longer than READ_TIMEOUT_MS, and
    does nothing to it or the page: no scroll, no focus, no wait for it to show. Its own timeout bounds the wait
    for the element, not a page too busy to answer, so the whole call is bounded here too; past the bound it is
    cancelled and left behind, not waited for.
    """
    call = asyncio.ensure_future(
        page.locator(selector).first.evaluate(DESCRIBE_TARGET_SCRIPT, _LABEL_CUT, timeout=READ_TIMEOUT_MS)
    )
    try:
        done, _ = await asyncio.wait({call}, timeout=READ_TIMEOUT_MS / 1000)
    finally:
        if not call.done():
            call.cancel()
            call.add_done_callback(_outcome_left_behind)
    if not done:
        raise TimeoutError(f"the element did not answer within {READ_TIMEOUT_MS} ms")
    return call.result()


def _outcome_left_behind(call: asyncio.Future[Any]) -> None:
    """Retrieve what a cancelled read ended with, so asyncio does not report it as never retrieved."""
    if not call.cancelled():
        call.exception()
