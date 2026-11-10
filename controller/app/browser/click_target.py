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
# Deliberately narrow: matched case-insensitively, as whole words, at the start of a line of a button's label
# (_label_kind has the rules). A comma in a phrase may be left out of the label. Bare "Close", "Cancel", "Dismiss",
# "OK", "Continue", "Next" and "Accept" are left out on purpose: they are on nearly every page and mostly harmless,
# and an approval on each would make the fast profile unusable. So are "Checkout" and "Proceed to checkout" (they
# open a page), "Subscribe", "Unsubscribe", "Submit", "Confirm", "Save changes" and "Clear".
CLICK_LABEL_PHRASES: dict[ApprovalKind, tuple[str, ...]] = {
    "payment": (
        "pay",
        "buy",
        "purchase",
        "place order",
        "place your order",
        "order now",
        "confirm order",
        "confirm payment",
        "confirm purchase",
        "confirm and pay",
        "complete order",
        "complete purchase",
        "submit order",
        "submit payment",
        "donate",
        "transfer",
    ),
    "destructive": (
        "delete",
        "remove",
        "erase",
        "permanently delete",
        "yes, delete",
        "yes, remove",
        "confirm delete",
        "confirm deletion",
        "empty trash",
        "cancel subscription",
        "close account",
        "deactivate",
    ),
    "account_change": ("change password", "update password", "reset password", "change email", "update email"),
    "post": ("publish", "post", "send"),
}
# Labels that start with a phrase of the table and are harmless all the same, matched the same way and before
# it: a filter or a basket line taken away, a page of past orders, a plan's name, a form field's name.
HARMLESS_LABEL_PHRASES: tuple[str, ...] = (
    "remove filter",
    "remove filters",
    "remove all filters",
    "remove from cart",
    "remove from basket",
    "remove from compare",
    "remove from wishlist",
    "remove from favorites",
    "remove from favourites",
    "purchase history",
    "purchase order",
    "purchase orders",
    "pay as you go",
    "post code",
)
# "Send" asks nothing when its label goes on to one of these words: a login form's "Send code", "Send me a login
# link", "Send reset link". Without it no login by a one-time code ran unattended.
HARMLESS_AFTER_SEND: tuple[str, ...] = ("code", "link", "otp", "passcode", "verification")
# A longer label is not a button's name but a sentence about one ("How to delete your account").
MAX_LABEL_CHARS = 40
# The read's bound, for the message that runs it and, apart, for the messages that set it up: past it the click
# goes on unclassified.
READ_TIMEOUT_MS = 2000
# The lines of a label that are read, each by itself: a button's name, then what is written under it.
MAX_LABEL_LINES = 3
# How much of each label the page hands over: room for the lines read, long ones too.
_LABEL_CUT = MAX_LABEL_CHARS * 10
_BUTTON_INPUT_TYPES = frozenset({"button", "submit", "image", "reset"})
# A phrase ends where its last word does: a hyphen goes on with the word ("Pay-as-you-go", "Post-processing").
_WORD_END = r"(?![\w\-\u2010\u2011])"


def _starting_with(phrases: tuple[str, ...]) -> str:
    return "^(?:" + "|".join(re.escape(phrase).replace(",", ",?") for phrase in phrases) + ")" + _WORD_END


_PHRASE_PATTERNS = {kind: re.compile(_starting_with(phrases)) for kind, phrases in CLICK_LABEL_PHRASES.items()}
_HARMLESS_PATTERN = re.compile(
    _starting_with(HARMLESS_LABEL_PHRASES)
    + "|"
    + _starting_with(("send",))
    + r"(?=.*\b(?:"
    + "|".join(re.escape(word) for word in HARMLESS_AFTER_SEND)
    + r")\b)"
)
_LEADING_SYMBOLS = re.compile(r"^[^\w]+")
# Characters that show nothing and would split a word from the table's: zero-width space, non-joiner and joiner,
# word joiner, byte order mark, soft hyphen.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))
# A token that is no word of the label: a number or a mark ("1", "\N{MIDDLE DOT}"), or an icon font's ligature
# name, which has an underscore ("shopping_cart"). Up to this many are skipped before the label's first word.
_NOT_A_WORD = re.compile(r"[\W\d]+|\S*_\S*")
_MAX_SKIPPED_TOKENS = 2

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
# (x, y) => the element a press at that point lands on, or null: the innermost one, through open shadow roots
# and into frames whose document the page's origin can reach (the same origin, srcdoc), up to three frames down.
# The point is carried into a frame by its box, border and padding; a frame that CSS scales or turns is read at a
# point that is off. A frame of another origin is as far as it goes: the element is the frame.
_ELEMENT_AT_SCRIPT = r"""
(x, y) => {
  let root = document;
  let el = null;
  for (let frames = 0; frames <= 3; frames += 1) {
    let hit = root.elementFromPoint(x, y);
    for (let depth = 0; hit && hit.shadowRoot && depth < 8; depth += 1) {
      const inner = hit.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    if (!hit) break;
    el = hit;
    if (el.localName !== 'iframe' && el.localName !== 'frame') break;
    let inside = null;
    try { inside = el.contentDocument; } catch (error) { inside = null; }
    if (!inside || !inside.defaultView) break;
    const box = el.getBoundingClientRect();
    const style = el.ownerDocument.defaultView.getComputedStyle(el);
    x -= box.left + el.clientLeft + (parseFloat(style.paddingLeft) || 0);
    y -= box.top + el.clientTop + (parseFloat(style.paddingTop) || 0);
    root = inside;
  }
  return el;
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
    el = ("""
    + _ELEMENT_AT_SCRIPT
    + r""")(arg.x, arg.y);
  }
  return ("""
    + DESCRIBE_TARGET_SCRIPT
    + r""")(el, arg.limit);
}
"""
)
# Runs in the isolated world: (arg) => whether a press at (otherX, otherY) lands on the very element one at (x, y)
# does, found as the classifier finds a point's.
SAME_ELEMENT_SCRIPT = (
    r"""
(arg) => {
  const elementAt = """
    + _ELEMENT_AT_SCRIPT
    + r""";
  return elementAt(arg.x, arg.y) === elementAt(arg.otherX, arg.otherY);
}
"""
)
# The comparison's bound, for its message and, apart, for the messages that set it up. Past it the press goes to
# the point itself, which costs nothing but the jitter, so it is short: the pointer is already there, waiting.
SAME_ELEMENT_TIMEOUT_MS = 500


def _label_kind(label: str) -> ApprovalKind | None:
    """The kind of the first phrase of the table a label starts with, read whole and line by line; None for none.

    The label is read as its whole text on one line (a phrase a line break splits is still one), and then each of
    its first MAX_LABEL_LINES lines by itself: a button whose name stands over a sentence about it is longer than
    a name, and was skipped whole.
    """
    lines = [line for line in label.translate(_INVISIBLE).splitlines() if line.strip()]
    for text in (" ".join(lines), *lines[:MAX_LABEL_LINES]) if len(lines) > 1 else lines:
        kind = _line_kind(text)
        if kind is not None:
            return kind
    return None


def _line_kind(line: str) -> ApprovalKind | None:
    shown = _LEADING_SYMBOLS.sub("", " ".join(line.split())).casefold()
    if not shown or len(shown) > MAX_LABEL_CHARS:
        return None
    shown = _from_its_first_word(shown)
    if _HARMLESS_PATTERN.match(shown):
        return None
    for kind, pattern in _PHRASE_PATTERNS.items():
        if pattern.match(shown):
            return kind
    return None


def _from_its_first_word(shown: str) -> str:
    """``shown`` without the tokens before its first word that are not words; one that is all the label has is
    read with its underscores as spaces ("delete_forever", an icon font's name for the icon it draws)."""
    tokens = shown.split(" ")
    skipped = 0
    while skipped < _MAX_SKIPPED_TOKENS and len(tokens) > 1 and _NOT_A_WORD.fullmatch(tokens[0]):
        tokens = tokens[1:]
        skipped += 1
    if len(tokens) == 1 and "_" in tokens[0]:
        return " ".join(tokens[0].replace("_", " ").split())
    return _LEADING_SYMBOLS.sub("", " ".join(tokens))


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


def classified_reason(kind: RiskCategory, label: RiskCategory | None, agent_reason: str) -> str:
    """The reason of the approval a click asks for when its target, not its label, gave it the kind ``kind``.

    Asked with the agent's reason alone, the operator read the account of the model that was steered ("opening the
    help panel") beside a kind of ``destructive``, and nothing said who chose the kind. The controller's sentence
    comes first, short enough for what a short account of an approval shows (APPROVAL_SUMMARY_CUT), and the
    agent's reason follows, named as the agent's. Nothing read from the page is in it.
    """
    return (
        f'The controller reads this click\'s target as a button of kind "{kind}"; '
        f'the agent labelled the click "{label}". The agent\'s reason: {agent_reason}'
    )


async def read_click_target(
    element_ids: ElementIds, page: Page, decision: BrowserActionDecision, *, session_id: str
) -> Any:
    """What the click ``decision`` makes would land on (DESCRIBE_TARGET_SCRIPT's answer), or None when it cannot be
    read. The target is named as the click names it: an element id, else a selector, else a point.

    A read that fails is logged at info, by session and by the kind of failure, since the click then runs as
    labelled: without the line nothing tells a click that was read and found harmless from one that was not read.
    The failure's text goes to debug only: it can hold text of the page.
    """
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
        logger.info(
            "session %s: the target of a %s click could not be read (%s); the click runs as labelled, unclassified",
            session_id,
            decision.risk_category,
            type(exc).__name__,
        )
        logger.debug("session %s: reading the target of a click failed with: %r", session_id, exc)
        return None


async def same_element_at(
    element_ids: ElementIds, page: Page, point: tuple[float, float], other: tuple[float, float]
) -> bool:
    """Whether a press at ``other`` lands on the element one at ``point`` does; False when that cannot be told.

    For a click by coordinates, whose press is moved a little from its point (click_human_like): the element at the
    point is the one that was read, and asked about when it needed an approval.
    """
    try:
        same = await element_ids.evaluate(
            page,
            SAME_ELEMENT_SCRIPT,
            {"x": point[0], "y": point[1], "otherX": other[0], "otherY": other[1]},
            timeout_ms=SAME_ELEMENT_TIMEOUT_MS,
            setup_timeout_ms=SAME_ELEMENT_TIMEOUT_MS,
            keep_session=True,
        )
    except Exception as exc:
        logger.debug("the elements at a click's point and at its jittered point could not be compared: %r", exc)
        return False
    return same is True


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
