"""What a fast-profile click lands on, read from the page, not taken from the model's label (STORE-42).

In the fast profile a decision's own ``risk_category`` decides whether its click asks for approval, and
``read``, ``write`` and, on a click, ``upload`` ask for nothing. That label is written by a model reading the page
it acts on, and text on the page can steer it to call a click on "Delete account" any of them. So before the
approval decision, a click so labelled has its target read: a button with a line of its label starting with a
phrase of CLICK_LABEL_PHRASES is recorded with that phrase's kind, and asks for approval as an honest label does,
with a reason that says the controller chose the kind (classified_reason); a form's submit button labelled
``read`` is recorded as ``write``, which the fast profile does not ask about. A type is read the same way: it puts
the caret in its target by a press on it. A target that is not there, or cannot be read (a page that does not
answer, which is logged at info), changes nothing: the action goes on as labelled, to succeed or fail by itself.

The target is found as the click will find it. One named by element id or by a point is read in the controller's
isolated world (element_world.py), where no page script runs; a point is followed through open shadow roots and
into frames of the page's own origin, and a click by coordinates keeps its jitter only while the same element is
under the moved point (same_element_at). One named by a selector is resolved by Playwright, as the click resolves
it (``page.locator(selector).first``: CSS that pierces open shadow roots, ``text=``, ``role=``, ``xpath=``, ``>>``
chains), and described where Playwright evaluates, which is where the page's own script runs. Reading it as CSS in
the isolated world missed every other dialect, and could read another element than the one clicked (for ``.a, .b``
Playwright's first match may sit in a shadow root that ``document.querySelector`` never enters). The text that
steers a model is content on an honest site, not the site's script; a page whose own script is hostile can falsify
this read, as it can name its button "Continue" or change it after any read.

The press is read once more (reading_presses, refuse_press_not_asked_about). What the action names is not always
what the mouse goes down on: the button that fills a wrapper the click named, one the page shows as the pointer
arrives, another match of a selector resolved a second time. With the pointer in place, what is under it is read in
the isolated world, and a button of a kind the action has no approval for is not pressed (PressNeedsApproval): the
action asks for that approval. Retried with it, the action takes its kind from the approval it presents, with no
read of the target before the decision, so a read that fails on the retry cannot run the click unasked and leave
the approval unspent.

Not caught: a label the table lacks (it is English); a frame of another origin; a control pressed by a key or a
drag; a clickable element that is not a button, and a link; a label given only by aria-labelledby, an image with
no alt text, or text that CSS or a canvas draws; a name past the cut of a label (_LABEL_CUT) or below its third
line; look-alike characters; a page whose script changes what is under the pointer between the read at the press
and the press. docs/production-hardening.md lists the table and these, and a test keeps its list the table's
(tests/test_click_target_doc.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..action_errors import BrowserActionError
from ..models import ApprovalKind, BrowserActionDecision, RiskCategory
from .element_ids import target_element_id

if TYPE_CHECKING:
    from playwright.async_api import Page

    from .element_ids import ElementIds

logger = logging.getLogger(__name__)

# The labels the fast profile asks no approval for on a click: a click with one has its target read. "upload"
# is one of them: only the upload action asks for that kind.
CHECKED_LABELS = frozenset({"read", "write", "upload"})
# The actions that press the mouse on a target the agent names: a click, and a type, which puts the caret in its
# target by a press on it, whatever the target is. Each has its target read.
TARGET_READ_ACTIONS = frozenset({"click", "type"})
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
    "remove from playlist",
    "remove from queue",
    "remove from watchlist",
    "remove from library",
    "remove from list",
    "remove from bag",
    "purchase history",
    "purchase order",
    "purchase orders",
    "pay as you go",
    "post code",
)
# "Send" asks nothing when what it sends is a way to sign in: a login form's "Send code", "Send me a login link",
# "Send reset link", "Send code to my phone". Without it no login by a one-time code ran unattended. Only words of
# SEND_FILLER_WORDS may stand between "send" and one of HARMLESS_AFTER_SEND, and only a word of SEND_TAIL_WORDS may
# follow it, so "Send payment link", "Send gift code" and "Send code review request" still ask.
#
# A link is a way to sign in only where one of SEND_LINK_WORDS stands before it. "Send link" is also the button of
# a share dialog, and what that sends is a document, to whomever the dialog's form names: text on the page can
# have the model fill in the form and press it.
HARMLESS_AFTER_SEND: tuple[str, ...] = (
    "code",
    "otp",
    "passcode",
    "one-time password",
    "one time password",
    "one-time pin",
    "one time pin",
    "verification email",
    "confirmation email",
    "activation email",
    "recovery email",
    "reset email",
    "login email",
    "sign-in email",
    "reset instructions",
)
SEND_FILLER_WORDS: tuple[str, ...] = (
    "me",
    "us",
    "a",
    "an",
    "the",
    "my",
    "new",
    "another",
    "login",
    "log-in",
    "log in",
    "sign-in",
    "sign in",
    "signin",
    "magic",
    "reset",
    "password",
    "verification",
    "confirmation",
    "activation",
    "security",
    "one-time",
    "one time",
    "single-use",
    "recovery",
    "authentication",
    "2fa",
    "mfa",
    "sms",
    "email",
    "text",
)
SEND_LINK_WORDS: tuple[str, ...] = (
    "me",
    "us",
    "login",
    "log-in",
    "log in",
    "sign-in",
    "sign in",
    "signin",
    "magic",
    "reset",
    "password",
    "verification",
    "confirmation",
    "activation",
    "one-time",
    "one time",
    "single-use",
    "recovery",
    "authentication",
)
SEND_TAIL_WORDS: tuple[str, ...] = ("to", "via", "by", "through", "using", "in", "on", "again", "now", "instead")
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


def _one_of(words: tuple[str, ...]) -> str:
    return "(?:" + "|".join(re.escape(word) for word in words) + ")"


_PHRASE_PATTERNS = {kind: re.compile(_starting_with(phrases)) for kind, phrases in CLICK_LABEL_PHRASES.items()}
# The label is matched with its spaces collapsed to one each, so a space here is any run of them.
_SEND_FILLERS = f"(?: {_one_of(SEND_FILLER_WORDS)})*"
_HARMLESS_PATTERN = re.compile(
    _starting_with(HARMLESS_LABEL_PHRASES)
    + "|"
    + _starting_with(("send",))
    + f"(?:{_SEND_FILLERS} {_one_of(HARMLESS_AFTER_SEND)}"
    + f"|{_SEND_FILLERS} {_one_of(SEND_LINK_WORDS)}{_SEND_FILLERS} link)"
    + f"(?:$| {_one_of(SEND_TAIL_WORDS)}{_WORD_END})"
)
_LEADING_SYMBOLS = re.compile(r"^[^\w]+")
# Characters that show nothing and would split a word from the table's: Unicode's format characters (zero-width
# space, joiner and non-joiner, word joiner, byte order mark, soft hyphen, the marks and isolates that set a text's
# direction, tags) and the combining grapheme joiner.
_FORMAT_CATEGORY = "Cf"
_GRAPHEME_JOINER = "\u034f"
# A token that is no word of the label: a number or a mark ("1", "\N{MIDDLE DOT}"), or an icon font's ligature
# name, which has an underscore ("shopping_cart"). Up to this many are skipped before the label's first word.
_NOT_A_WORD = re.compile(r"[\W\d]+|\S*_\S*")
# A word all in lower case, as an icon font's name of one word is ("payment", "lock", "warning").
_LOWER_CASE_WORD = re.compile(r"[a-z][a-z0-9]*")
_MAX_SKIPPED_TOKENS = 2

# (el, limit) => what a click on el lands on, or null. The element counts as the button, input or link it sits in,
# as a click on it is one on them. Both reads below describe their element with it, so they cannot drift apart.
DESCRIBE_TARGET_SCRIPT = r"""
(el, limit) => {
  if (!el || el.nodeType !== 1) return null;
  // The button the element sits in, looked for along the tree as it is shown: out of a shadow root to its host
  // (the glyph of an icon component sits in the button its host sits in), and from what a page slots into a
  // component to the slot's place in that component's tree (a design system's button is a shadow button around
  // a slot). closest() sees neither.
  const BUTTONS = 'button, input, [role="button" i], a[href]';
  let target = null;
  for (let node = el, steps = 0; node && steps < 256; steps += 1) {
    if (node.nodeType === 1 && node.matches(BUTTONS)) { target = node; break; }
    node = node.assignedSlot || node.parentNode || node.host || null;
  }
  // A press on a label is one on its control: a label for a button, or around one, is that button, and its text
  // one of the button's names.
  let labelled = '';
  const label = target ? null : el.closest('label');
  if (label && label.control && label.control.matches('button, input')) {
    target = label.control;
    labelled = label.innerText;
  }
  // A component that is a button: all the page gives it sits, slotted, in one button of its own tree. Text given
  // to it with no element around is under the pointer as the component, and the walk above starts outside its tree.
  if (!target && el.shadowRoot) {
    const given = Array.from(el.childNodes).filter(
      (child) => child.nodeType === 1 || (child.nodeType === 3 && child.nodeValue.trim() !== '')
    );
    let inner = null;
    for (const child of given.length <= 64 ? given : []) {
      let found = null;
      for (let node = child.assignedSlot, steps = 0; node && node.nodeType === 1 && steps < 256; steps += 1) {
        if (node.matches(BUTTONS)) { found = node; break; }
        node = node.assignedSlot || node.parentNode;
      }
      if (!found || (inner && inner !== found)) { inner = null; break; }
      inner = found;
    }
    target = inner;
  }
  target = target || el;
  const tag = target.localName;
  const type = tag === 'button' || tag === 'input' ? String(target.type || '').toLowerCase() : '';
  const role = (target.getAttribute('role') || '').trim().split(/\s+/)[0].toLowerCase();
  const submits = (tag === 'button' && type === 'submit')
    || (tag === 'input' && (type === 'submit' || type === 'image'));
  const cut = (value) => (typeof value === 'string' ? value.slice(0, limit) : '');
  const labels = [cut(target.getAttribute('aria-label')), cut(target.innerText), cut(target.getAttribute('title'))];
  if (tag === 'input') labels.push(cut(target.value), cut(target.getAttribute('alt')));
  if (labelled) labels.push(cut(labelled));
  // An icon button is often named on what is inside it: a label, a title or alt text on the icon.
  const named = target.querySelectorAll('[aria-label], [title], [alt]');
  for (let index = 0; index < named.length && index < 4; index += 1) {
    for (const name of ['aria-label', 'title', 'alt']) labels.push(cut(named[index].getAttribute(name)));
  }
  // innerText leaves out what a slot or a shadow root inside the button shows. That text is gathered here, a
  // line for each block as innerText makes them, and handed over only when there is such a slot or root.
  let crossed = false;
  let budget = 256;
  const shown = [];
  const gather = (node) => {
    if (budget <= 0) return;
    budget -= 1;
    if (node.nodeType === 3) { shown.push(node.nodeValue.replace(/\s+/g, ' ')); return; }
    if (node.nodeType === 11) { node.childNodes.forEach(gather); return; }
    if (node.nodeType !== 1) return;
    const name = node.localName;
    if (name === 'script' || name === 'style' || name === 'template') return;
    if (name === 'br') { shown.push('\n'); return; }
    const display = node.ownerDocument.defaultView.getComputedStyle(node).display;
    if (display === 'none') return;
    const block = !/^(inline|contents)/.test(display);
    if (block) shown.push('\n');
    const assigned = name === 'slot' ? node.assignedNodes({ flatten: true }) : [];
    if (assigned.length) {
      crossed = true;
      assigned.forEach(gather);
    } else if (node.shadowRoot) {
      crossed = true;
      gather(node.shadowRoot);
    } else {
      node.childNodes.forEach(gather);
    }
    if (block) shown.push('\n');
  };
  gather(target);
  if (crossed) labels.push(cut(shown.join('')));
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
# does, found as the classifier finds a point's. Not where that cannot be told: nothing at the point, or a frame
# or plugin the read stops at (another origin's, or a fourth frame down), inside which two points may be two
# controls.
SAME_ELEMENT_SCRIPT = (
    r"""
(arg) => {
  const elementAt = """
    + _ELEMENT_AT_SCRIPT
    + r""";
  const el = elementAt(arg.x, arg.y);
  if (!el || ['iframe', 'frame', 'object', 'embed'].includes(el.localName)) return false;
  return el === elementAt(arg.otherX, arg.otherY);
}
"""
)
# The comparison's bound, for its message and, apart, for the messages that set it up. Past it the press goes to
# the point itself, which costs nothing but the jitter, so it is short: the pointer is already there, waiting.
SAME_ELEMENT_TIMEOUT_MS = 500
# The bound of the read made just before a press, for its message and, apart, for the messages that set it
# up. Short for the same reason: past it the press goes on.
PRESS_READ_TIMEOUT_MS = 500


def _label_kind(label: str) -> ApprovalKind | None:
    """The kind of the first phrase of the table a label starts with, read whole and line by line; None for none.

    The label is read as its whole text on one line (a phrase a line break splits is still one), and then each of
    its first MAX_LABEL_LINES lines by itself: a button whose name stands over a sentence about it is longer than
    a name, and was skipped whole. A line the table matches is let go when the label from that line on is a
    harmless continuation: "Remove" over "filter" is "Remove filter", however the page breaks it.
    """
    lines = _lines(label)
    kind = _line_kind(" ".join(lines)) if len(lines) > 1 else None
    if kind is not None:
        return kind
    for index, line in enumerate(lines[:MAX_LABEL_LINES]):
        kind = _line_kind(line, " ".join(lines[index + 1 :]).casefold())
        if kind is not None:
            return kind
    return None


def _lines(label: str) -> list[str]:
    """The lines of a label that show something, each as it is matched but for its case: its spaces collapsed,
    the marks before its first word gone, what shows nothing taken out."""
    shown = (_LEADING_SYMBOLS.sub("", " ".join(line.split())) for line in _without_invisible(label).splitlines())
    return [line for line in shown if line]


def _without_invisible(label: str) -> str:
    return "".join(
        char for char in label if char != _GRAPHEME_JOINER and unicodedata.category(char) != _FORMAT_CATEGORY
    )


def _line_kind(shown: str, rest: str = "") -> ApprovalKind | None:
    """The kind a line asks for; ``rest`` is what the label goes on with after it, case folded.

    Each reading of the line (_readings) is matched by itself, and one that is a harmless continuation, alone or
    with the rest of the label, asks nothing.
    """
    if not shown or len(shown) > MAX_LABEL_CHARS:
        return None
    for reading in _readings(shown):
        if _HARMLESS_PATTERN.match(reading) or (rest and _HARMLESS_PATTERN.match(f"{reading} {rest}")):
            continue
        for kind, pattern in _PHRASE_PATTERNS.items():
            if pattern.match(reading):
                return kind
    return None


def _harmless(shown: str) -> bool:
    """Whether a text, read from its first word, is a harmless continuation of a phrase of the table."""
    return _HARMLESS_PATTERN.match(_readings(shown)[-1]) is not None


def _readings(shown: str) -> list[str]:
    """What a line is matched as, case folded. The line asks when any of them does.

    Tokens before the first word that are no words are skipped (a number, a mark, an icon font's name with an
    underscore). Then, where one or two words all in lower case lead up to a capitalised one, the line is read
    from that one as well: an icon font's name of one word is such a word, put on the label's line by the icon's
    element ("payment Pay now"), and hid the label. And from its first word, as before: "delete Account" is a
    label too, so a harmless label after an icon whose name is a phrase of the table ("delete Remove filter")
    still asks. A line that is one token with underscores and no dot is read with them as spaces
    ("delete_forever", the name of the icon it draws; "delete_me.txt" is a file's name).
    """
    tokens = shown.split(" ")
    skipped = 0
    while skipped < _MAX_SKIPPED_TOKENS and len(tokens) > 1 and _NOT_A_WORD.fullmatch(tokens[0]):
        tokens = tokens[1:]
        skipped += 1
    if len(tokens) == 1 and "_" in tokens[0] and "." not in tokens[0]:
        return [" ".join(tokens[0].replace("_", " ").split()).casefold()]
    lead = 0
    while lead < _MAX_SKIPPED_TOKENS - skipped and lead + 1 < len(tokens) and _LOWER_CASE_WORD.fullmatch(tokens[lead]):
        lead += 1
    whole = _LEADING_SYMBOLS.sub("", " ".join(tokens)).casefold()
    if lead and tokens[lead][:1].isupper():
        return [" ".join(tokens[lead:]).casefold(), whole]
    return [whole]


def classified_risk(target: Any, label: RiskCategory | None) -> RiskCategory | None:
    """What a click labelled ``label`` on ``target`` (DESCRIBE_TARGET_SCRIPT's answer) is recorded as; None keeps it."""
    if not isinstance(target, dict) or label not in CHECKED_LABELS:
        return None
    tag, kind_of_input, role = (str(target.get(name) or "") for name in ("tag", "type", "role"))
    button_like = tag == "button" or (tag == "input" and kind_of_input in _BUTTON_INPUT_TYPES) or role == "button"
    if button_like:
        labels = target.get("labels")
        labels = [text for text in labels if isinstance(text, str)] if isinstance(labels, list) else []
        for text in labels:
            kind = _label_kind(text)
            if kind is not None and not _said_in_full_elsewhere(text, labels):
                return kind
    if target.get("submits") is True and label == "read":
        return "write"
    return None


def _said_in_full_elsewhere(label: str, labels: list[str]) -> bool:
    """Whether another name of the same button goes on from ``label``'s very words to a harmless continuation.

    A button drawn as "Send" and named "Send code" for a screen reader, a filter chip drawn as "Remove": the
    fuller name is the button's, and the short one alone asked.
    """
    said = " ".join(_lines(label)).casefold()
    for other in labels:
        full = " ".join(_lines(other))
        if full.casefold().startswith(said + " ") and _harmless(full):
            return True
    return False


def classified_reason(kind: RiskCategory, label: RiskCategory | None, agent_reason: str, action: str = "click") -> str:
    """The reason of the approval a click (or a type) asks for when its target, not its label, gave it ``kind``.

    Asked with the agent's reason alone, the operator read the account of the model that was steered ("opening the
    help panel") beside a kind of ``destructive``, and nothing said who chose the kind. The controller's sentence
    comes first, short enough for what a short account of an approval shows (APPROVAL_SUMMARY_CUT), and the
    agent's reason follows, named as the agent's. Nothing read from the page is in it.
    """
    what = "click" if action == "click" else f"{action} action"
    return (
        f'The controller reads this {what}\'s target as a button of kind "{kind}"; '
        f'the agent labelled the {what} "{label}". The agent\'s reason: {agent_reason}'
    )


async def read_click_target(
    element_ids: ElementIds, page: Page, decision: BrowserActionDecision, *, session_id: str
) -> Any:
    """What the press ``decision`` makes (a click's, or a type's on its target) would land on, as
    DESCRIBE_TARGET_SCRIPT answers, or None when it cannot be read. The target is named as the action names it: an
    element id, else a selector, else a point.

    A read that fails is logged at info, by session and by the kind of failure, since the action then goes on as
    labelled: without the line nothing tells one that was read and found harmless from one that was not read. The
    failure's text goes to debug only: it can hold text of the page.
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
        # "Goes on", not "runs": a target that cannot be read may not be there for the action either.
        logger.info(
            "session %s: the target of a %s labelled %s could not be read (%s); it goes on as labelled, unclassified",
            session_id,
            decision.action,
            decision.risk_category,
            type(exc).__name__,
        )
        logger.debug("session %s: reading the target of a %s failed with: %r", session_id, decision.action, exc)
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


@dataclass(frozen=True)
class _PressRead:
    """What a press made for the action under way is read against: the action, the label the agent gave it, and
    the kind the approval it runs under was given for (None when it runs under none)."""

    action: str
    label: RiskCategory | None
    approved: ApprovalKind | None


# Set around an action whose target was read (BrowserActionService.execute_decision). A press made for anything
# else is not read: a direct call of the click route, a governed run under its operator's approval.
_PRESS_READ: ContextVar[_PressRead | None] = ContextVar("press_read", default=None)


@contextmanager
def reading_presses(action: str, label: RiskCategory | None, approved: ApprovalKind | None) -> Iterator[None]:
    """While the block runs, every press is read first (refuse_press_not_asked_about)."""
    token = _PRESS_READ.set(_PressRead(action, label, approved))
    try:
        yield
    finally:
        _PRESS_READ.reset(token)


def press_is_read() -> bool:
    """Whether a press made now is read first: inside reading_presses."""
    return _PRESS_READ.get() is not None


class PressNeedsApproval(BrowserActionError):
    """A press stopped before the mouse went down: under the pointer is a button of a kind (``kind``) the action
    has no approval for. BrowserActionService.execute_decision asks for that approval."""

    def __init__(self, kind: ApprovalKind, action: str) -> None:
        super().__init__(
            f'Nothing was pressed: the element under the pointer reads as a button of kind "{kind}", which this '
            f"{action} has no approval for.",
            code="press_needs_approval",
            action=action,
            status_code=409,
            retryable=False,
        )
        self.kind = kind


async def refuse_press_not_asked_about(
    element_ids: ElementIds, page: Page, x: float, y: float, *, session_id: str
) -> None:
    """Read what is under the pointer at (x, y), where the mouse is about to go down, and refuse the press
    (PressNeedsApproval) when it is a button of a kind of the table the action under way has no approval for.

    The read before the approval decision describes the element the action names, as the page stood then. The
    press lands on what is under the pointer: the button that fills a wrapper the action named, or one the page
    showed as the pointer arrived (a row's "Delete", shown while the pointer is over the row). Outside
    reading_presses nothing is read. What cannot be read is pressed, as a target that cannot be read goes on, and
    is logged the same way.
    """
    reading = _PRESS_READ.get()
    if reading is None:
        return
    try:
        target = await element_ids.evaluate(
            page,
            CLICK_TARGET_SCRIPT,
            {"x": x, "y": y, "limit": _LABEL_CUT},
            timeout_ms=PRESS_READ_TIMEOUT_MS,
            setup_timeout_ms=PRESS_READ_TIMEOUT_MS,
            keep_session=True,
        )
    except Exception as exc:
        logger.info(
            "session %s: what is under the pointer could not be read before the press of a %s labelled %s (%s); "
            "the press goes on, unclassified",
            session_id,
            reading.action,
            reading.label,
            type(exc).__name__,
        )
        logger.debug("session %s: reading what is under the pointer failed with: %r", session_id, exc)
        return
    kind = classified_risk(target, reading.label)
    if kind in CLICK_LABEL_PHRASES and kind != reading.approved:
        raise PressNeedsApproval(kind, reading.action)


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
        # On every way out, an ended read too: a click cancelled just as its read failed left the failure, the
        # page's text in it, for asyncio to report.
        call.add_done_callback(_outcome_left_behind)
    if not done:
        raise TimeoutError(f"the element did not answer within {READ_TIMEOUT_MS} ms")
    return call.result()


def _outcome_left_behind(call: asyncio.Future[Any]) -> None:
    """Retrieve what a read ended with, so asyncio does not report it as never retrieved."""
    if not call.cancelled():
        call.exception()
