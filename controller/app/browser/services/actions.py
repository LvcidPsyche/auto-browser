from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ... import events as _events
from ...action_errors import BrowserActionError, SessionNotFoundError
from ...actions import ActionRunContext
from ...approval_display import approval_provenance, approval_summary
from ...approvals import (
    CONTROLLER_SCOPE,
    ApprovalRequiredError,
    held_action_approval,
    held_for_execution,
    held_tool_call_approval,
    refused_before_effect,
)
from ...models import (
    READ_ONLY_ACTIONS,
    ApprovalFieldRead,
    ApprovalKind,
    ApprovalOrigin,
    BrowserActionDecision,
    totp_host_allowed,
)
from ...utils import error_text, error_text_for_log, spawn_background_task
from ...webhooks import dispatch_approval_event
from ...witness import WitnessApproval
from ..click_target import (
    CHECKED_LABELS,
    CLICK_LABEL_PHRASES,
    TARGET_READ_ACTIONS,
    PressNeedsApproval,
    classified_reason,
    classified_risk,
    press_is_read,
    read_click_target,
    reading_presses,
    refuse_press_not_asked_about,
    same_element_at,
)
from ..element_ids import selector_hint, target_element_id
from ..element_scripts import (
    ELEMENT_HAS_FOCUS_SCRIPT,
    FIELD_HOLD_READ_SCRIPT,
    FIELD_HOLD_RELEASE_SCRIPT,
    FIELD_HOLD_TAKE_SCRIPT,
)
from ..element_world import READ
from ..page_read import title_or_blank
from .element_actions import FOCUS_CLICK_TIMEOUT_MS, JITTER_PX, ElementActions
from .witness import action_witness_context

try:  # pragma: no cover - optional until dependency is installed in runtime image
    import pyotp
except Exception:  # pragma: no cover - graceful fallback for non-login test runs
    pyotp = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...browser_manager import BrowserSession
    from ...pii_scrub import PiiScrubber

logger = logging.getLogger(__name__)

# A placed click the controller refused before its press: nothing was pressed.
TARGET_MOVED = "target_moved"


def _target_left_the_pointer() -> BrowserActionError:
    """The target had no box any more once Playwright's checks had passed, or once it had put the pointer on it."""
    return BrowserActionError(
        "Nothing was pressed: the target is no longer shown. Observe the page again and retry.",
        code=TARGET_MOVED,
        status_code=409,
        retryable=True,
    )


def _target_not_under_the_pointer() -> BrowserActionError:
    """Where Playwright's hover had put the pointer, the pointer did not find the target: the page took it away
    since, something lies over it there, or it is in a frame the page has something over."""
    return BrowserActionError(
        "Nothing was pressed: where the click was placed, the pointer does not find the target. Observe the page "
        "again and retry.",
        code=TARGET_MOVED,
        status_code=409,
        retryable=True,
    )


def _target_has_no_box() -> BrowserActionError:
    """The target has no box of its own (display: contents): Playwright places a click on it on one of its children.

    A position is counted from the corner of a box, so there is none to give Playwright, and the point it picks
    itself it does not tell: where the press would land cannot be read.
    """
    return BrowserActionError(
        "Nothing was pressed: the target has no box of its own (its children are laid out in its place), so where a "
        "press on it would land cannot be read. Name an element inside it.",
        code="target_has_no_box",
        status_code=409,
        retryable=False,
    )


# Times the human-like mouse path's 4-18 ms gaps. perf_counter, not the event
# loop's clock: that is time.monotonic, which ticks every 15.6 ms on Windows
# before Python 3.13, coarser than what is being measured.
_step_clock = time.perf_counter

# Whether the element (or a label for it) is what the pointer at (x, y) hits, in the element's own document.
# For selector targets only: an element named by element_id takes a stricter check in the isolated world
# (element_target.py), where a label for it does not count.
# (x, y) is a point of the main frame's viewport. In a child frame the script cannot tell where its document
# lies in that viewport, and answered yes for every point: "checked by Playwright", whose checks are made at
# Playwright's own point, the middle of what the viewport shows of the element, and not at the middle of its
# box, where the press goes. So there it answers 'framed' until it is given ``box``, the element's box in the
# main frame's viewport (Playwright's bounding_box): the point is carried into the document by the two boxes,
# in proportion, which is exact where the frames on the way are moved or scaled, and at the middle of the box
# where one is turned; not where a frame on the way runs in a process of its own and has a border or padding
# (``arrived``, below). _receives_pointer_at asks it of the element, then of each frame element on the way up.
# The way up from what the pointer hits goes by the slot a node is given to, as a click's event does: a label
# slotted into a component's button is that button's. By its parents alone it was not, which only sent the
# click to Playwright's placing while nothing asked the target after that; the same question is asked there now.
# ``arrived``: the pointer is at (x, y) now, and the element, in a frame, is asked as well whether the pointer is
# over it as the browser has it (:hover). The box that carries the point is Playwright's, and in a frame that is
# in a process of its own (another site's, a sandboxed one) Playwright counts a box from the corner of the frame's
# border: with a border or padding there, every box is up and left of its element by that much, the point was
# carried by that same box, and the answer was yes wherever the pointer was. No box comes into :hover. It is
# asked inside :is(), since a document in quirks mode matches a bare :hover to links alone, and of the label
# where a label stands for the element. Before a human-like press only, where a no hands the click to
# Playwright's placing: a yes can be an old one (a document is not told of a pointer that went on into another
# process's, so the rest of the question stands), and a no a wrong one (a person moving the real pointer over a
# headed browser).
# ``wide``: the element is the target of a click Playwright placed, and it is asked as Playwright's own check of a
# click asks it: an element inside a button or a link is taken for that button or link (its retarget,
# "button-link"), unless it is a field or editable text. A selector that names a card's text names a node, and where
# another piece of the card's link lay over that node (a span stretched over the card, a ::before over a button) the
# pointer found the link or the button and not the node: Playwright's hover said yes, this question said no, and a
# no ends a placed click. Never of a frame on the way up, which must itself be what the pointer finds, and not
# before a human-like press, where a no only hands the click to the placing.
# ``box`` is ``true`` where (x, y) is already a point of el's own document: where that document saw the pointer
# arrive (_POINTER_WATCH_SCRIPT), for the target of a placed click in a frame. Nothing is carried then.
# ``onlyFrames``, given only where it is true: that document saw no move, so the pointer is over another one, and only a
# frame that is el or inside it (a video in a card) can be what the pointer finds there. Playwright's own check of a
# click takes the pointer over a frame inside its target for the target.
_RECEIVES_POINTER_SCRIPT = """(el, [x, y, box, arrived, wide, onlyFrames]) => {
  const over = (found) => !(arrived && window !== window.top) || found.matches(':is(:hover)');
  const field = el.matches('input, textarea, select') || el.isContentEditable;
  const target = (wide && !field && el.closest('button, [role=button], a, [role=link]')) || el;
  if (window !== window.top && box !== true) {
    if (!box) return 'framed';
    const own = el.getBoundingClientRect();
    if (!(own.width > 0 && own.height > 0 && box.width > 0 && box.height > 0)) return false;
    x = own.left + ((x - box.x) * own.width) / box.width;
    y = own.top + ((y - box.y) * own.height) / box.height;
  }
  let hit = document.elementFromPoint(x, y);
  while (hit && hit.shadowRoot) {
    const inner = hit.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  if (!hit) return false;
  if (onlyFrames && !hit.matches('iframe, frame, object, embed')) return false;
  const label = hit.closest ? hit.closest('label') : null;
  if (label && label.control === el) return over(label);
  for (let node = hit; node; node = node.assignedSlot || node.parentNode || node.host) {
    if (node === target) return over(target);
  }
  return false;
}"""

# (el) => a record of the next move of the pointer that el's document sees: ``at`` is null until one comes, then
# the point it came to, in that document's own viewport, and false where the document runs no script (a frame
# sandboxed without allow-scripts: a mail's body), which runs no listener either though it answers the driver's
# questions: it is asked with a made-up event, of an EventTarget of its own, which nothing of the page's hears.
# Set up for the target of a placed click inside a frame, once Playwright's hover is done and before the
# controller puts the pointer on the same point itself (a move to the point the pointer is on is sent to the page
# as any other). The browser hands a move to the one document under the pointer, in that document's own
# coordinates, whatever lies between it and the screen: a frame's border and padding, a frame of another process,
# a turn. So the record is where the pointer is, where the target's two boxes only told where Playwright counts it
# to be. A trusted pointermove, heard on the way down, so that a listener of the page's that stops it at the target
# does not keep it from the record; or the mousemove after it, where a listener the page put on the window first
# stops the pointermove there (a pointermove has the point in fractions of a pixel, a mousemove in whole ones). The
# listener takes one and goes, or goes when the record is read (_POINTER_SEEN_SCRIPT), or when the click ends
# (_POINTER_WATCH_STOP_SCRIPT). Where no script runs, it is never called.
_POINTER_WATCH_SCRIPT = """(el) => {
  const view = el.ownerDocument.defaultView;
  const seen = { at: false };
  const probe = new EventTarget();
  probe.addEventListener('watch', () => { seen.at = null; });
  probe.dispatchEvent(new Event('watch'));
  const note = (event) => {
    if (!event.isTrusted) return;
    seen.at = [event.clientX, event.clientY];
    seen.stop();
  };
  const moves = ['pointermove', 'mousemove'];
  seen.stop = () => moves.forEach((type) => view.removeEventListener(type, note, true));
  moves.forEach((type) => view.addEventListener(type, note, true));
  return seen;
}"""

# (el, seen) => the record of _POINTER_WATCH_SCRIPT, read in el's own document (the driver refuses a handle of
# another), and the end of its watch.
_POINTER_SEEN_SCRIPT = "(el, seen) => { seen.stop(); return seen.at; }"
# (seen) => the end of the watch, where the click ends with its record not read.
_POINTER_WATCH_STOP_SCRIPT = "(seen) => seen.stop()"

# (el) => the point Playwright's click picks on el, as the ``position`` that names it: the middle of what the
# viewport shows of the first of el's boxes that it shows a part of (an inline element broken over lines
# has a box a line, and one around a block an empty box before and after the block: the first box whatever its
# size was the corner pixel of such a card), counted from the corner of its padding box, with the widths of the
# border at that corner. Playwright presses a position where it is given, so the controller knows the point; the
# point Playwright picks itself it cannot ask for. The middle is of what is shown along each axis, however
# thin: taken so only where an axis showed a whole pixel, a wide card with 0.9 px of its height on the screen
# (area enough for Playwright to pick it) was aimed at above the screen. Of a box the viewport shows nothing
# of, it is the middle of the box, which Playwright's hover scrolls to.
# ``screen`` is [the box Playwright finds for el, in the main frame's viewport; that viewport's size, asked for
# in a frame only]. Playwright counts a position from the box it finds: the box around the whole of el, turned
# with it. That is not always the box around el's boxes as the screen has them (getBoundingClientRect): an
# element of several boxes inside something CSS turns or skews has the two apart, 26 px on a link over two
# lines turned 30 degrees. Counted from the script's own box, the position named a point beside such a link,
# and Playwright's hover waited the click's time out there, where its own click pressed the link. In the main
# frame the place is counted from Playwright's box where that box is not the script's size. Of one size they
# are one box (Playwright's holds the script's), and the script's own is the one to count from: it is read in
# the run that picks the point, and Playwright's a round trip earlier, so a target the page moved between the
# two was aimed at off by its move, and the hover waited the click's time out. ``counted``: the place is counted
# from Playwright's box, which is then read again once the script has run (_place_of). In a frame the two boxes
# are in different coordinates, and the place is counted from the script's own: such a link in a frame is a limit
# the docs name.
# In a frame the script sees what the frame shows of el, which is not what Playwright goes by: it cuts a box
# by the screen alone, whatever a frame on the way cuts off, and the click's checks have just passed at that
# point. So there it answers ``framed``, and is asked again with the size of the main frame's viewport as
# well: what is shown is then what the screen shows, and no longer what the frame does. Cut by both, a card
# link wider than its frame was placed at the middle of what the frame shows, a point nothing had checked;
# with something of the frame's own there Playwright's hover waited the click's time out, where its own
# click pressed the link. (Where the frame does not show Playwright's point, the checks have failed before
# this is asked, as Playwright's own click does.) The two boxes of el differ in size where a frame on the
# way is scaled: the place is then in the screen's pixels, as Playwright counts a position from the box it
# finds on the screen (in the frame's own pixels the point was twice as far along in a frame drawn at half
# its size).
# ``boxless``: el has no box at all, so there is no corner to count from; ``contents``: because its children are
# laid out in its place (display: contents), and not because it is not shown.
_PRESS_PLACE_SCRIPT = """(el, screen) => {
  const view = el.ownerDocument.defaultView;
  const framed = view !== view.top;
  const style = view.getComputedStyle(el);
  const border = [parseInt(style.borderLeftWidth, 10) || 0, parseInt(style.borderTopWidth, 10) || 0];
  const whole = el.getBoundingClientRect();
  const boxes = Array.from(el.getClientRects());
  const [seen, size] = screen || [];
  let [left, top, right, bottom] = [0, 0, view.innerWidth, view.innerHeight];
  let [wider, taller] = [1, 1];
  let corner = [whole.left, whole.top];
  const sized = (box) => Math.abs(box.width - whole.width) < 0.5 && Math.abs(box.height - whole.height) < 0.5;
  const counted = Boolean(seen && !framed && !sized(seen));
  if (counted) {
    corner = [seen.x, seen.y];
  } else if (seen && framed && size && whole.width > 0 && whole.height > 0 && seen.width > 0 && seen.height > 0) {
    [wider, taller] = [seen.width / whole.width, seen.height / whole.height];
    const across = (x) => whole.left + (x - seen.x) / wider, down = (y) => whole.top + (y - seen.y) / taller;
    [left, top, right, bottom] = [across(0), down(0), across(size[0]), down(size[1])];
  }
  // As Playwright takes the first quad with an area over 0.99 inside the viewport, measured here on the rectangle
  // round each box: a long thin box of less, turned, has a rectangle of more (a limit the docs name).
  const shown = (box) =>
    Math.max(0, Math.min(box.right, right) - Math.max(box.left, left)) *
    Math.max(0, Math.min(box.bottom, bottom) - Math.max(box.top, top));
  const picked = boxes.find((box) => shown(box) > 0.99);
  const first = picked || boxes[0] || whole;
  const middle = (low, high, from, to) => (picked ? (Math.max(low, from) + Math.min(high, to)) / 2 : (low + high) / 2);
  return {
    x: (middle(first.left, first.right, left, right) - corner[0]) * wider - border[0],
    y: (middle(first.top, first.bottom, top, bottom) - corner[1]) * taller - border[1],
    border,
    boxless: boxes.length === 0,
    contents: style.display === 'contents',
    framed,
    counted,
  };
}"""

# How long a click waits before it looks again at a target that is not enabled yet. Playwright's own click waits
# 20 to 500 ms between its looks.
ENABLED_POLL_SECONDS = 0.1
# How many times a placed click counts its place again from a box the page changed while the place was read
# (_place_of).
PLACE_RECOUNTS = 2
# What Playwright says of an element the page took out of the document while a step was waiting on it.
_NOT_ATTACHED = "Element is not attached to the DOM"
# The check that the pointer finds a target inside frames (_receives_pointer_at): how many documents it asks, the
# target's own first, and how long it waits for the element it asks about. A target seven frames down is asked
# all the way up. Deeper, the check before a human-like press cannot tell and gives the click to Playwright's own
# placing; asked again where Playwright has placed the pointer, it goes by what the documents it asked have said.
POINTER_CHECK_FRAMES = 8
POINTER_CHECK_TIMEOUT_MS = 1000
# How far the target's box of a placed click in a frame may have moved or changed its size, each way, between the
# placing and the question, for the record of where the pointer arrived to be taken (_receives_pointer_at's
# ``placed_by``).
PLACED_BOX_MOVED_PX = 0.5


async def _each_on_its_own(steps: list[Callable[[], Awaitable[Any]]]) -> None:
    """Run each step of a give-back (a handle's dispose, a watch's stop), whatever the one before did.

    A step that fails is let go: what it gives back goes with the page. A cancellation (it is no Exception) at one
    skipped the rest, which the page then kept for good: it is raised once every step has run.
    """
    cancelled: asyncio.CancelledError | None = None
    for step in steps:
        try:
            with contextlib.suppress(Exception):
                await step()
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
    if cancelled is not None:
        raise cancelled


def _box_kept(box: dict[str, float] | None, placed_by: dict[str, float] | None) -> bool:
    """Whether ``box`` is where ``placed_by`` was and of its size, to PLACED_BOX_MOVED_PX each way: a frame scaled
    about the target's corner keeps the corner of its box."""
    if not box or not placed_by:
        return False
    return all(abs(box[edge] - placed_by[edge]) <= PLACED_BOX_MOVED_PX for edge in ("x", "y", "width", "height"))


class _Budget:
    """What is left of a click's budget, for each of its waits in turn.

    Given the whole budget each, the waits of one click could hold its session for several budgets, on a target
    that kept every one of them waiting to the end: Playwright's own click has one deadline for all its checks.

    It bounds the click's waits for its target, and nothing else. A page that stops answering holds a click
    still: the pointer's moves and the questions asked of a handle wait for the page, with no bound to give
    them. A page busy for 6 seconds once the checks had passed held a click for 7 under a budget of 3, and the
    click then ended as a timeout with nothing pressed; one busy for 7 seconds from the mouse going down held it
    for 8, and the click pressed.
    """

    def __init__(self, timeout_ms: float) -> None:
        self.timeout_ms = timeout_ms
        self._ends = time.monotonic() + timeout_ms / 1000

    @property
    def spent(self) -> bool:
        return time.monotonic() >= self._ends

    def left_ms(self, at_most: int | None = None) -> int:
        # Never 0, which Playwright reads as no limit at all: a wait begun with nothing left has a millisecond,
        # and ends as the timeout it is. ``at_most``: for a wait that has a bound of its own within the budget.
        left = max(1, round((self._ends - time.monotonic()) * 1000))
        return left if at_most is None else min(left, at_most)


# Total time type_text_human_like may spend typing, keystrokes and pauses together. Longer text is typed faster, not
# longer: 5000 characters at the default delays would hold the session lock for about 11 minutes. Past the budget the
# rest goes in as plain text (insert_text), its newlines and tabs pressed as keys.
HUMAN_TYPING_BUDGET_SECONDS = 20.0
# The least a keystroke or the final insert is given once the budget is spent, before it counts as hung. The worst
# case is about 24 s, not 22: a keystroke started just before the deadline may take this long past it, and the rest
# of the text is then bounded by max(deadline, now) + this.
HUMAN_TYPING_STEP_FLOOR_SECONDS = 2.0
_CONTROL_KEYS = {"\n": "Enter", "\r": "Enter", "\t": "Tab"}
_CONTROL_KEY_PATTERN = re.compile("([" + "".join(_CONTROL_KEYS) + "])")

# Where the TOTP autofill looks for a one-time-code field, most specific first.
# Every candidate must still pass is_one_time_code_field.
_ONE_TIME_CODE_FIELD_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[id*="otp" i]',
    'input[inputmode="numeric"][maxlength="6"]',
    'input[name*="code" i]',
    'input[id*="code" i]',
    'input[aria-label*="code" i]',
    'input[placeholder*="code" i]',
)
_MAX_CODE_FIELD_CANDIDATES = 5
_ONE_TIME_CODE_INPUT_TYPES = frozenset({"", "text", "tel", "number", "password"})
# Words that say a field holds some other kind of code. The autofill typed live
# one-time codes into promo, ZIP and card-security fields because their names
# contain "code"; missing a real code field only means the agent types it.
_NOT_A_ONE_TIME_CODE = re.compile(
    r"zip|postal|postcode|promo|coupon|discount|voucher|gift|referr|invite|country|area|region|"
    r"phone|\btel\b|currency|locale|lang|product|sku|order|track|captcha|cvv|cvc|csc|card|cc-",
    re.IGNORECASE,
)
# What a type's target says of its text where the text is shown (text_target_payload), and is withheld until the
# field is known not to be secret (withhold_text_preview, give_back_text_preview).
_TEXT_PREVIEW_KEYS = ("text_preview", "text_truncated")
# How long a type's hold on the node its text goes to waits (_TypedField): by selector for the whole take (the field
# found, which had focus a moment before, the hold made on it and its first read), and from then on for the page's
# answer to each read and give-back. No read waits for a field once the text is in: a hold on a node the page took
# away (the text's Enter submitted its form) fails at once, where a read by selector waited this long for each
# attribute of a field that was gone.
# And how long the read of a type's field for its approval takes at most, before the approval is asked for
# (_approval_field_read): one that has not answered by then leaves the field unread.
FIELD_RECHECK_TIMEOUT_MS = 1000
_FIELD_ATTRIBUTES_SCRIPT = """(el) => ({
  type: el.getAttribute('type') || '',
  name: el.getAttribute('name') || '',
  id: el.id || '',
  placeholder: el.getAttribute('placeholder') || '',
  'aria-label': el.getAttribute('aria-label') || '',
  autocomplete: el.getAttribute('autocomplete') || '',
})"""

# What an action's result says of a tab the action opened (NAV-08): a page's new tab does not become the active one.
NEW_TAB_MESSAGE = (
    "The action opened this tab. Observations and actions stay on the active tab until browser.activate_tab (REST: "
    "POST /sessions/{session_id}/tabs/activate) moves to this index."
)


# Actions that are in ActionName but that nothing executes, with why.
_NOT_EXECUTABLE = {
    "request_human_takeover": (
        "request_human_takeover is not an action to execute; use browser.request_human_takeover"
    ),
    "done": "done is not an action to execute: it ends an agent's run",
}


def _refuse_unrunnable(decision: BrowserActionDecision) -> None:
    """Refuse an action nothing executes, before any approval is asked for.

    A takeover has its own tool and route, and done ends an agent's run, so the
    only use of an approval for either was to put agent-written text before an
    operator, shaped like the gateway's stand-in for a governed tool call.
    """
    refusal = _NOT_EXECUTABLE.get(decision.action)
    if refusal is not None:
        raise ValueError(refusal)


def is_one_time_code_field(attributes: Mapping[str, Any]) -> bool:
    """Whether an input's own attributes say it takes a one-time code."""
    if str(attributes.get("type") or "").strip().lower() not in _ONE_TIME_CODE_INPUT_TYPES:
        return False
    if str(attributes.get("autocomplete") or "").strip().lower() == "one-time-code":
        return True
    described = " ".join(
        str(attributes.get(key) or "") for key in ("name", "id", "placeholder", "aria-label", "autocomplete")
    )
    return not _NOT_A_ONE_TIME_CODE.search(described)


def _tab_count(snapshot: dict[str, Any]) -> int | None:
    """How many tabs a snapshot saw: an action snapshot counts them, an observation lists them; None if neither."""
    count = snapshot.get("tab_count")
    if isinstance(count, int):
        return count
    tabs = snapshot.get("tabs")
    return len(tabs) if isinstance(tabs, list) else None


def _reads_not_secret(attributes: Mapping[str, Any] | None) -> bool:
    """Whether a read through a type's hold says its node is not secret; None is no read (no node held, or one that
    did not keep focus)."""
    return attributes is not None and not BrowserActionService.is_sensitive_field(attributes)


async def _answered_soon(call: Awaitable[Any]) -> Any:
    """``call``'s answer, within FIELD_RECHECK_TIMEOUT_MS: a question asked of a handle has no bound of its own (a
    page that stops answering would hold the type, and its session, with it), and the reads of a field for its
    approval are bounded one by one, not together."""
    return await asyncio.wait_for(call, FIELD_RECHECK_TIMEOUT_MS / 1000)


def _left_until(ends: float) -> float:
    """The seconds left until the loop's time ``ends``: none once it has passed."""
    return max(0.0, ends - asyncio.get_running_loop().time())


async def _arrives_by(call: Awaitable[Any], ends: float, give_back: Callable[[Any], Coroutine[Any, Any, None]]) -> Any:
    """The handle ``call`` answers, where it answers by the loop's time ``ends``: else the call's error, or the
    bound's.

    The call is a task of its own, which giving up on it (the bound passed, the caller was cancelled) does not
    cancel. A cancelled call does not stop the page: the handle is made all the same, and the answer that carries it
    finds nobody to give it back. One that arrives after it was given up on is given back as it arrives, by
    ``give_back``, in the background: nothing waits for that.
    """
    step = asyncio.ensure_future(call)
    try:
        return await asyncio.wait_for(asyncio.shield(step), _left_until(ends))
    except BaseException:
        # Whether or not the step is done: one that finished in the iteration its caller was cancelled had its
        # handle dropped by shield, and a done task still runs this callback (soon).
        _GIVEN_UP.add(step)
        step.add_done_callback(lambda arrived: _given_back_late(arrived, give_back))
        raise


# The steps given up on that have not answered yet: the loop keeps only a weak reference to a task.
_GIVEN_UP: set["asyncio.Future[Any]"] = set()


def _given_back_late(arrived: "asyncio.Future[Any]", give_back: Callable[[Any], Coroutine[Any, Any, None]]) -> None:
    _GIVEN_UP.discard(arrived)
    if arrived.cancelled() or arrived.exception() is not None:
        return  # nothing arrived: the step failed, as its take already has
    spawn_background_task(give_back(arrived.result()))


async def _disposed_soon(handle: Any) -> None:
    """Give a handle back, within the bound: one the page does not answer for goes with its document."""
    with contextlib.suppress(Exception):
        await _answered_soon(handle.dispose())


async def _hold_released_soon(hold: Any) -> None:
    """Give back a hold nothing reads any more: by its release script first, which takes its listeners off the node,
    and then the handle, each within the bound and on its own."""
    await _each_on_its_own(
        [lambda: _answered_soon(hold.evaluate(FIELD_HOLD_RELEASE_SCRIPT)), lambda: _answered_soon(hold.dispose())]
    )


class _TypedField:
    """A type's hold on the node its text goes to, taken just before the first keystroke and given back once the text
    is in: the innermost focused element in the field the type names (element_scripts._HELD_FIELD_JS).

    ``not_secret`` says whether the text's preview may be shown: the node read as not secret through the hold when it
    was taken and again as it was given back, and kept focus in between. Read by the type's selector or element_id
    instead, the field was found anew each time: a key of the text that sent focus on to a password field left the
    rest of the text there unread, and by selector a field turned secret and then replaced read as its replacement.

    By element_id the hold is the element ids' world's, under a nonce, and a take that fails raises, as the element's
    other steps do. By selector it is a handle of the page's, made on the handle of the element the selector finds
    and then read: three calls of the type's own, within FIELD_RECHECK_TIMEOUT_MS together, and a handle that arrives
    once its call was given up on (the bound passed, the type was cancelled) is given back as it arrives
    (_arrives_by). In one Locator.evaluate_handle the timeout bounded only the wait for the element, and the hold was
    the type's only once Playwright had disposed of the element's handle, with no bound either: a page that did not
    answer held the type, and a type cancelled then had made a hold it could not give back. A hold that cannot be
    taken or read says secret, the text typed all the same, as when the field's attributes could not be read. On
    either path every call after the take answers within FIELD_RECHECK_TIMEOUT_MS or counts as failed.
    """

    def __init__(self, element: Any, locator: Any) -> None:
        self._element = element
        self._locator = locator
        self._nonce = secrets.token_hex(16)
        # By selector: the handle of the element the selector found, and the hold made on it.
        self._found: Any = None
        self._handle: Any = None
        self.not_secret = False

    async def take(self) -> None:
        if self._element is not None:
            self.not_secret = _reads_not_secret(await self._element.hold_field(self._nonce))
            return
        ends = asyncio.get_running_loop().time() + FIELD_RECHECK_TIMEOUT_MS / 1000
        try:
            finding = self._locator.element_handle(timeout=FIELD_RECHECK_TIMEOUT_MS)
            self._found = await _arrives_by(finding, ends, _disposed_soon)
            holding = self._found.evaluate_handle(FIELD_HOLD_TAKE_SCRIPT)
            self._handle = await _arrives_by(holding, ends, _hold_released_soon)
            read = self._handle.evaluate(FIELD_HOLD_READ_SCRIPT)
            self.not_secret = _reads_not_secret(await asyncio.wait_for(read, _left_until(ends)))
        except Exception:
            self.not_secret = False

    async def release(self) -> None:
        """Read the node once more and give the hold back, whatever ended the type: each step on its own, and a
        cancellation raised once all have run (_each_on_its_own). A step that fails says secret.

        What the controller holds is given back whatever the page does: by selector both handles are disposed of.
        The page's side goes with the release script, which takes the hold's listeners off the node and, by
        element_id, the hold out of the world's fieldHolds. Where that script never runs (a page that does not
        answer it), the node keeps the listeners, and the world its entry, until the document goes. Nothing reads
        such a hold: each type takes its own, by its own handle or under its own nonce.
        """
        still_not_secret = False

        async def read_again() -> None:
            nonlocal still_not_secret
            if self._element is not None:
                attributes = await self._element.release_field(self._nonce, within_ms=FIELD_RECHECK_TIMEOUT_MS)
            else:
                attributes = await _answered_soon(self._handle.evaluate(FIELD_HOLD_RELEASE_SCRIPT))
            still_not_secret = _reads_not_secret(attributes)

        steps: list[Any] = []
        if self._element is not None or self._handle is not None:
            steps.append(read_again)
        for handle in (self._handle, self._found):
            if handle is not None:
                steps.append(lambda handle=handle: _answered_soon(handle.dispose()))
        await _each_on_its_own(steps)
        self.not_secret = self.not_secret and still_not_secret


class BrowserActionService:
    """Encapsulates browser action execution and approval orchestration."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.elements = ElementActions(self)

    async def navigate(self, session_id: str, url: str) -> dict[str, Any]:
        self.manager._assert_url_allowed(url)
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            # The agent's navigation: it clears a refused page's taint when it ends on the list, judged
            # by its response (PageProvenance.controller_navigation). A tainted page's commit during it
            # still starts the page guard, which waits for the lock and then finds nothing to do.
            with self.manager.page_provenance.controller_navigation(session.page) as navigation:
                navigation.response = await session.page.goto(url, wait_until="domcontentloaded")
            # The navigate may have cleared the page's taint. Close every window that page opened while it was
            # refused that Playwright never reported, now, before the settle gives a script it planted time to
            # commit an allowed URL or send this tab to one (window_births.close_unreported closes only windows
            # whose opener is clean or gone), and again after the settle for one that became unjustified during it.
            await self.manager.window_births.close_unreported(session)
            await self.manager._settle(session.page)
            spawn_background_task(self.manager.window_births.close_unreported(session))
            # A bot challenge it lands on is the pipeline's to find and hand over, as after every action.

        return await self.manager._run_action(session, "navigate", {"url": url}, operation)

    async def click(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        target = self.resolve_target(selector=selector, element_id=element_id, x=x, y=y)

        async def operation() -> None:
            if target["mode"] == "coordinates":
                await self.click_human_like(session, float(x), float(y))
            elif target.get("element_id"):
                element = self.manager.element_ids.element(session.page, target["element_id"])
                target["x"], target["y"] = await self.elements.click(session, element)
            else:
                locator = session.page.locator(target["selector"]).first
                # One budget for every wait of the click, as Playwright's own click has one timeout. The scroll
                # and the read of the box each waited by Playwright's default, a whole budget more apiece.
                budget = self._budget(None)
                await self._scroll_into_view(locator, budget)
                coords = await self.locator_center(locator, timeout_ms=budget.left_ms())
                if coords is None:
                    await self.click_as_playwright_places_it(session, locator, budget=budget)
                else:
                    target["x"], target["y"] = coords
                    await self.click_locator_human_like(session, locator, coords, budget=budget)
            await self.manager._settle(session.page)

        return await self.manager._run_action(session, "click", target, operation)

    async def hover(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        target = self.resolve_target(selector=selector, element_id=element_id, x=x, y=y)

        async def operation() -> None:
            if target["mode"] == "coordinates":
                await self.move_mouse_human_like(session, float(x), float(y))
            elif target.get("element_id"):
                element = self.manager.element_ids.element(session.page, target["element_id"])
                target["x"], target["y"] = await self.elements.hover(session, element)
            else:
                locator = session.page.locator(target["selector"]).first
                await locator.scroll_into_view_if_needed()
                coords = await self.locator_center(locator)
                if coords is None:
                    await locator.hover()
                else:
                    target["x"], target["y"] = coords
                    await self.move_mouse_human_like(session, coords[0], coords[1])
            await self.manager._settle(session.page)

        return await self.manager._run_action(session, "hover", target, operation)

    async def select_option(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        value: str | None = None,
        label: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        target = self.resolve_target(selector=selector, element_id=element_id)

        async def operation() -> None:
            if target.get("element_id"):
                element = self.manager.element_ids.element(session.page, target["element_id"])
                await element.select_option(value=value, label=label, index=index)
                await self.manager._settle(session.page)
                return
            locator = session.page.locator(target["selector"]).first
            await locator.scroll_into_view_if_needed()
            if index is not None:
                await locator.select_option(index=index)
            elif value is not None:
                await locator.select_option(value=value)
            else:
                await locator.select_option(label=label)
            await self.manager._settle(session.page)

        return await self.manager._run_action(
            session,
            "select_option",
            {**target, "value": value, "label": label, "index": index},
            operation,
        )

    async def type(
        self,
        session_id: str,
        *,
        text: str,
        selector: str | None = None,
        element_id: str | None = None,
        clear_first: bool = True,
        sensitive: bool = False,
    ) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        target = self.resolve_target(selector=selector, element_id=element_id)
        payload = self.text_target_payload(
            target,
            text,
            clear_first=clear_first,
            sensitive=sensitive,
            preview_chars=80,
            scrubber=self.manager.pii_scrubber,
        )
        preview = {key: payload[key] for key in _TEXT_PREVIEW_KEYS if key in payload}
        # Withheld until a read of the field's attributes has said it is not secret, from before the pipeline: a type
        # it refuses before the operation runs (a policy block, a page ALLOWED_HOSTS refuses) reads no field, and
        # withheld only as the operation began, its blocked record kept the preview under a standard receipt.
        self.withhold_text_preview(payload)

        async def operation() -> None:
            element = locator = None
            if target.get("element_id"):
                element = self.manager.element_ids.element(session.page, target["element_id"])
                await self.elements.focus(session, element)
            else:
                locator = session.page.locator(target["selector"]).first
                await locator.scroll_into_view_if_needed()
                await self.focus_verified(session, locator, action="type")
            if clear_first:
                await session.page.keyboard.press("Control+a")
                await asyncio.sleep(0.03)
                if element is not None:
                    # A select-all handler can move focus and select another field, which Delete would empty.
                    await self.elements.require_focus(element, during=" after select-all (nothing was cleared)")
                await session.page.keyboard.press("Delete")
                await asyncio.sleep(0.05)
                if element is not None:
                    # A page can move focus once the clear has gone in, before the text does.
                    await self.elements.require_focus(element, during=" through the clear")
            # Read just before the first keystroke, and again once the text is in: a page can make the field a
            # password field as it is focused or cleared (a login form that does so as it is focused), or as the text
            # goes in. Read before the focus, a field turned so kept the preview of the text typed into it. Both
            # reads are of the node that has focus as the text begins, held from then until the text is in
            # (_TypedField): a key of the text can send focus on to another field, which takes the rest.
            field = _TypedField(element, locator)
            try:
                await field.take()
                await self.type_text_human_like(session.page, text)
            finally:
                await field.release()
            if field.not_secret:
                self.give_back_text_preview(payload, preview)
            await self.manager._settle(session.page)

        return await self.manager._run_action(session, "type", payload, operation)

    async def press(self, session_id: str, key: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            await session.page.keyboard.press(key)
            await self.manager._settle(session.page)

        return await self.manager._run_action(session, "press", {"key": key}, operation)

    async def scroll(self, session_id: str, delta_x: float, delta_y: float) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            await session.page.mouse.wheel(delta_x, delta_y)
            await self.manager._settle(session.page)

        return await self.manager._run_action(
            session,
            "scroll",
            {"delta_x": delta_x, "delta_y": delta_y},
            operation,
        )

    async def wait(self, session_id: str, wait_ms: int) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            await asyncio.sleep(max(0, wait_ms) / 1000)

        return await self.manager._run_action(session, "wait", {"wait_ms": wait_ms}, operation)

    async def reload(self, session_id: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            await session.page.reload(wait_until="domcontentloaded")
            await self.manager._settle(session.page)

        return await self.manager._run_action(session, "reload", {}, operation)

    async def go_back(self, session_id: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            await session.page.go_back(wait_until="domcontentloaded")
            await self.manager._settle(session.page)

        return await self.manager._run_action(session, "go_back", {}, operation)

    async def go_forward(self, session_id: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            await session.page.go_forward(wait_until="domcontentloaded")
            await self.manager._settle(session.page)

        return await self.manager._run_action(session, "go_forward", {}, operation)

    async def execute_decision(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            session = await self.manager.get_session(session_id)
        except SessionNotFoundError as exc:
            # Nothing ran: an approval an outer layer claimed for this action (execute_approval's, whose
            # session closed after its claim) is given back.
            refused_before_effect(exc)
            raise
        try:
            _refuse_unrunnable(decision)
            self._refuse_a_host_off_the_list(decision)
            if decision.action == "upload":
                # Check the file before asking anyone to approve the upload: an
                # operator should not be asked to approve an upload of a file that
                # is not there, only for it to fail once they have. The direct
                # upload path already checks in this order.
                await self.manager.uploads.safe_path(decision.file_path or "", session=session)
        except Exception as exc:
            # Nothing ran: refused here under an outer layer's claim (a governed call's, its file gone since
            # the gateway checked it), that approval is given back.
            refused_before_effect(exc)
            raise
        # Before the target's read gives the decision another kind: the agent's own label, and whether the
        # action is one whose target is read at all.
        label = decision.risk_category
        reads_its_target = self._reads_its_target(decision)
        decision, asked_for_its_target = await self._click_risk_from_its_target(
            session, decision, approval_id=approval_id
        )
        approval = await self.require_decision_approval(
            session_id,
            decision,
            approval_id=approval_id,
            fallback_reason=asked_for_its_target,
        )
        # The receipt names the approval the action runs under: this call's own, or
        # the one an outer layer holds for it (a governed call's governed_approval_id).
        # An id the caller presented on an action that needs no approval is shown (status None, required False):
        # the audit trail of what was presented, not a claim that anything approved it.
        held = held_action_approval()
        witnessed_id = approval_id or (approval.id if approval is not None else None) or held
        witness_context = {
            "risk_category": decision.risk_category,
            "approval_id": witnessed_id,
            "approval_status": "approved" if witnessed_id else None,
            "runtime_requires_approval": approval is not None,
            "approval_checked": approval is not None or held is not None,
            "sensitive_input": bool(getattr(decision, "sensitive", False)),
        }
        # An action whose target was read has each press read once more, at the pointer, against the kind
        # it runs approved for (click_target.refuse_press_not_asked_about).
        approved = approval.kind if approval is not None else None
        presses = reading_presses(decision.action, label, approved) if reads_its_target else nullcontext()
        try:
            with action_witness_context(witness_context), presses:
                # Uploads go through manager.upload, which checks and claims the
                # approval itself.
                if approval is None or decision.action == "upload":
                    return await self._dispatch_recorded(session_id, decision, approval_id=approval_id)
                async with held_for_execution(self.manager.approvals, approval.id, may_give_back=True):
                    return await self._dispatch_recorded(session_id, decision, approval_id=approval_id)
        except PressNeedsApproval as stopped:
            # Stopped with the pointer over a button of a kind nobody was asked about (the action named its
            # wrapper, or the page showed it as the pointer arrived): asked for now, as the read before the
            # decision asks. An approval the action ran under is spent: its action had begun.
            asks = decision.model_copy(update={"risk_category": stopped.kind})
            await self.require_decision_approval(
                session_id,
                asks,
                approval_id=None,
                fallback_reason=classified_reason(
                    stopped.kind, label, decision.reason, decision.action, at_the_press=True
                ),
            )
            raise

    async def _click_risk_from_its_target(
        self, session: "BrowserSession", decision: BrowserActionDecision, *, approval_id: str | None = None
    ) -> tuple[BrowserActionDecision, str | None]:
        """A click labelled read, write or upload, as what it lands on records it (click_target.py), and the
        reason of the approval it asks for when its target gave it a kind that asks (None otherwise). A type
        too: it puts the caret in its target by a press on it, and aimed at a button it pressed the button.

        Before the approval decision, so a button the model gave such a label that deletes, pays, posts or
        changes the account asks for approval as an honest label would, and a form submit called read is recorded
        as a write. Not when the caller presents the approval this very action asked for: it runs under that
        one.
        """
        if not self._reads_its_target(decision):
            return decision, None
        presented = await self._kind_of_the_approval_presented(session, decision, approval_id)
        if presented is not None:
            return decision.model_copy(update={"risk_category": presented}), None
        target = await read_click_target(self.manager.element_ids, session.page, decision, session_id=session.id)
        risk = classified_risk(target, decision.risk_category)
        if risk is None:
            return decision, None
        # The operator is asked in the controller's words: the agent's reason alone is the steered model's.
        reason = (
            classified_reason(risk, decision.risk_category, decision.reason, decision.action)
            if risk in CLICK_LABEL_PHRASES
            else None
        )
        return decision.model_copy(update={"risk_category": risk}), reason

    @staticmethod
    def _reads_its_target(decision: BrowserActionDecision) -> bool:
        """Whether ``decision`` presses a target that is read first, and again at each press: a click or a
        type whose label asks for nothing in the fast profile.

        Not inside an outer layer's approval: a governed run's, which an operator gave for this very action, or
        the one execute_approval runs, whose action was read when it was asked for.
        """
        return (
            decision.action in TARGET_READ_ACTIONS
            and decision.risk_category in CHECKED_LABELS
            and held_action_approval() is None
            and held_tool_call_approval() is None
        )

    async def _kind_of_the_approval_presented(
        self, session: "BrowserSession", decision: BrowserActionDecision, approval_id: str | None
    ) -> ApprovalKind | None:
        """The kind of the approval the caller presents, when that approval was asked for this very action as
        a button of a kind of the table; None for any other id, which changes nothing.

        The retry read its target anew, and a target that could not be read then (a busy page) gave it no
        kind: it ran asking for nothing, the approval it presented stayed approved, and that approval ran the
        action again. The action now takes its kind from the approval, which require_approved then checks
        and the run spends, whatever the target reads as by now.
        """
        if not approval_id:
            return None
        try:
            approval = await self.manager.approvals.get(approval_id)
        except KeyError:
            return None
        if approval.session_id != session.id or approval.origin != "action":
            return None
        if approval.kind not in CLICK_LABEL_PHRASES:
            return None
        asked = decision.model_copy(update={"risk_category": approval.kind})
        return approval.kind if self.manager.approvals.covers(approval, asked) else None

    def _refuse_a_host_off_the_list(self, decision: BrowserActionDecision) -> None:
        """Refuse a navigate whose URL ALLOWED_HOSTS refuses, before an approval for it is asked for or spent.

        navigate checks the list only once it runs, so an operator was asked to approve a navigation that
        could never run, and the approval was then spent with nothing run. Refused after an outer layer's
        claim (a host off the list since the approve), that approval is given back.
        """
        if decision.action != "navigate":
            return
        try:
            self.manager._assert_url_allowed(decision.url or "")
        except PermissionError as exc:
            refused_before_effect(exc)
            raise

    async def _dispatch_recorded(
        self, session_id: str, decision: BrowserActionDecision, *, approval_id: str | None
    ) -> dict[str, Any]:
        """_dispatch_decision, with a failure before the action's effect recorded for the approval's holder.

        The dispatched action looks the session up again, outside its pipeline: a close landing while this
        call read the approval (a worker thread) was raised there unrecorded, and a claimed approval stayed
        spent with nothing run. refused_before_effect marks only holds whose effect has not begun.
        """
        try:
            return await self._dispatch_decision(session_id, decision, approval_id=approval_id)
        except Exception as exc:
            refused_before_effect(exc)
            raise

    async def _dispatch_decision(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None,
    ) -> dict[str, Any]:
        if decision.action == "navigate":
            return await self.manager.navigate(session_id, decision.url or "")
        if decision.action in {"click", "hover"}:
            run = self.manager.click if decision.action == "click" else self.manager.hover
            return await run(
                session_id,
                selector=decision.selector,
                element_id=decision.element_id,
                x=decision.x,
                y=decision.y,
            )
        if decision.action == "select_option":
            return await self.manager.select_option(
                session_id,
                selector=decision.selector,
                element_id=decision.element_id,
                value=decision.value,
                label=decision.label,
                index=decision.index,
            )
        if decision.action == "type":
            return await self.manager.type(
                session_id,
                selector=decision.selector,
                element_id=decision.element_id,
                text=decision.text or "",
                clear_first=decision.clear_first,
                sensitive=decision.sensitive,
            )
        if decision.action == "press":
            return await self.manager.press(session_id, decision.key or "")
        if decision.action == "scroll":
            return await self.manager.scroll(session_id, decision.delta_x, decision.delta_y)
        if decision.action == "wait":
            return await self.manager.wait(session_id, decision.wait_ms)
        if decision.action == "reload":
            return await self.manager.reload(session_id)
        if decision.action == "go_back":
            return await self.manager.go_back(session_id)
        if decision.action == "go_forward":
            return await self.manager.go_forward(session_id)
        if decision.action == "upload":
            return await self.manager.upload(
                session_id,
                selector=decision.selector,
                element_id=decision.element_id,
                file_path=decision.file_path or "",
                approved=False,
                approval_id=approval_id,
            )
        raise ValueError(f"Unsupported action: {decision.action}")  # pragma: no cover - guarded by schema

    async def require_decision_approval(
        self,
        session_id: str | None,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None,
        fallback_reason: str | None = None,
        approval_kind: ApprovalKind | None = None,
        origin: ApprovalOrigin = "action",
        arguments: str | None = None,
    ):
        """Require an approval for ``decision``; ``session_id=None`` asks in CONTROLLER_SCOPE.

        A tool call that names no session (a memory profile, a CDP attach) has
        no session to observe or witness, and none to look up an owner from: its
        approval belongs to the operator who asked (ApprovalRecord.owner).

        ``origin`` is "tool_call" only for the gateway's stand-in for a governed
        tool call (McpToolGateway._governed_call_decision), and ``arguments``
        that call's whole arguments, which its approval keeps beside the record.

        All of it runs before the action begins. Inside an outer layer's hold (a
        governed execute_action whose runtime approval is still to ask for, is
        refused, or names no approval that exists), whatever it raises gives the
        outer approval back: an unknown id used to spend it.
        """
        try:
            return await self._require_decision_approval(
                session_id,
                decision,
                approval_id=approval_id,
                fallback_reason=fallback_reason,
                approval_kind=approval_kind,
                origin=origin,
                arguments=arguments,
            )
        except Exception as exc:
            refused_before_effect(exc)
            raise

    async def _require_decision_approval(
        self,
        session_id: str | None,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None,
        fallback_reason: str | None,
        approval_kind: ApprovalKind | None,
        origin: ApprovalOrigin,
        arguments: str | None,
    ):
        if session_id == CONTROLLER_SCOPE:
            # The sentinel is a scope, not a session a caller may name: left to
            # match, it would let a session-bound call spend a controller approval.
            raise SessionNotFoundError(session_id)
        # Before the kind: a done that needs no approval (risk "read") is still nothing to run.
        if origin == "action":
            _refuse_unrunnable(decision)
        kind = approval_kind or self.approval_kind_for_decision(decision)
        if kind is None:
            return None
        if approval_id:
            return await self.manager.approvals.require_approved(
                approval_id=approval_id,
                session_id=session_id or CONTROLLER_SCOPE,
                kind=kind,
                action=decision,
                origin=origin,
            )

        session = None if session_id is None else await self.manager.get_session(session_id)
        approval = await self.manager.approvals.create_or_reuse_pending(
            session_id=session_id or CONTROLLER_SCOPE,
            kind=kind,
            reason=fallback_reason or decision.reason,
            action=decision,
            observation=None if session is None else await self._approval_observation(session),
            origin=origin,
            arguments=arguments,
            field_read=await self._approval_field_read(session, decision),
        )
        if session is None:
            # No session means no witness receipt to carry the request, so the
            # audit log does. The reason stays out: it previews the call's
            # arguments and the audit log is not scoped to an operator.
            await self.manager.audit.append(
                event_type="approval",
                status="pending",
                action="approval_requested",
                session_id=CONTROLLER_SCOPE,
                approval_id=approval.id,
                details={"kind": approval.kind, **approval_provenance(approval)},
            )
        else:
            await self.manager._record_witness_receipt(
                session,
                event_type="approval",
                status="pending",
                action="approval_requested",
                action_class="control",
                risk_category=decision.risk_category,
                approval=WitnessApproval(
                    required=True,
                    approval_id=approval.id,
                    status=approval.status,
                    reason=approval.reason,
                ),
                target={
                    "kind": approval.kind,
                    "action": decision.action,
                    "selector": decision.selector,
                    "element_id": decision.element_id,
                    **approval_provenance(approval),
                },
                metadata={"reason": approval.reason},
                approved_action=decision,
            )
            _events.emit_approval(
                session_id,
                approval.id,
                approval.kind,
                approval.status,
                approval.reason,
                origin=approval.origin,
                summary=approval_summary(approval),
                arguments_size=approval.arguments_size,
            )
        if self.manager.settings.approval_webhook_url:
            spawn_background_task(
                dispatch_approval_event(
                    approval,
                    webhook_url=self.manager.settings.approval_webhook_url,
                    webhook_secret=self.manager.settings.approval_webhook_secret,
                    allow_private_targets=self.manager.settings.approval_webhook_allow_private_targets,
                )
            )
        raise ApprovalRequiredError(approval)

    async def require_governed_approval(
        self,
        session_id: str | None,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None,
        reason: str | None = None,
        origin: ApprovalOrigin = "action",
        arguments: str | None = None,
    ):
        kind = self.governed_approval_kind_for_decision(decision)
        if kind is None:
            # A done that calls itself "read" needs no approval, and is still nothing to run.
            if origin == "action":
                _refuse_unrunnable(decision)
            return None
        self._refuse_a_host_off_the_list(decision)
        # A caller that knows what is being approved (the MCP gateway, for tool
        # calls that are not browser actions) supplies the reason the operator
        # reads; the generic sentence named the stand-in action instead.
        reason = reason or (
            "Governed workflow requires operator approval before executing "
            f"{decision.risk_category or 'write'} action {decision.action!r}."
        )
        return await self.require_decision_approval(
            session_id,
            decision,
            approval_id=approval_id,
            fallback_reason=reason,
            approval_kind=kind,
            origin=origin,
            arguments=arguments,
        )

    async def _approval_field_read(
        self, session: "BrowserSession | None", decision: BrowserActionDecision
    ) -> ApprovalFieldRead:
        """How the field of the type an approval is asked for reads (ApprovalRecord.field_read): "plain" or "secret"
        by its attributes, "unread" where the read does not answer. Unread too, and nothing read: an action that is
        no type, and a type the caller marked sensitive (that text is hidden and stored as a digest already).

        The approval is asked for before the type runs, so before any read of its field, and its summary (the
        webhook's, the SSE event's), what GET /approvals answers and its webhook carried the text whole. The field
        read is the one the selector or the element_id names, as it is now: no node has focus for the text yet (the
        type reads the node it holds: _TypedField). The whole read answers within FIELD_RECHECK_TIMEOUT_MS, the
        approval not yet asked for: by selector it waited ACTION_TIMEOUT_MS for a field not on the page.
        """
        if session is None or decision.action != "type" or decision.sensitive:
            return "unread"
        try:
            attributes = await _answered_soon(self._named_field_attributes(session.page, decision))
        except Exception:
            return "unread"
        return "secret" if self.is_sensitive_field(attributes) else "plain"

    async def _named_field_attributes(self, page: "Page", decision: BrowserActionDecision) -> Mapping[str, Any]:
        """The attributes is_sensitive_field reads, of the field ``decision`` names, by element_id or by selector."""
        target = self.resolve_target(selector=decision.selector, element_id=decision.element_id)
        if target.get("element_id"):
            element = self.manager.element_ids.element(page, target["element_id"])
            return await element.evaluate(_FIELD_ATTRIBUTES_SCRIPT, effect=READ, within_ms=FIELD_RECHECK_TIMEOUT_MS)
        locator = page.locator(target["selector"]).first
        return {
            name: await locator.get_attribute(name, timeout=FIELD_RECHECK_TIMEOUT_MS)
            for name in ("type", "name", "id", "autocomplete", "placeholder", "aria-label")
        }

    async def _approval_observation(self, session: "BrowserSession") -> dict[str, Any]:
        return {
            "url": self.manager._shown_url(session.page),
            "title": await title_or_blank(self.manager, session),
            "takeover_url": self.manager._current_takeover_url(session),
            "remote_access": self.manager.remote_access.session_info(session),
            "isolation": self.manager.session_lifecycle.isolation_payload(session),
            "auth_state": self.manager.auth_profiles.session_auth_state_info(session),
            "last_action": session.last_action,
        }

    async def settle(self, page: "Page") -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=min(self.manager.settings.action_timeout_ms, 5000))
        except Exception as exc:
            # Busy pages legitimately never reach networkidle; proceed anyway.
            logger.debug("settle: networkidle not reached: %s", error_text_for_log(exc))
        await page.wait_for_timeout(250)

    async def run_action(
        self,
        session: "BrowserSession",
        action_name: str,
        target: dict[str, Any],
        operation: Any,
        *,
        change_made: Any = None,
        began: Any = None,
    ) -> dict[str, Any]:
        context = ActionRunContext(
            manager=self.manager,
            session=session,
            action_name=action_name,
            target=target,
            operation=operation,
        )
        if change_made is not None:
            context.change_made = change_made
        if began is not None:
            context.began = began
        return await self.manager.action_pipeline.run(context)

    @staticmethod
    def text_target_payload(
        target: dict[str, Any],
        text: str,
        *,
        clear_first: bool,
        sensitive: bool,
        preview_chars: int,
        scrubber: PiiScrubber | None = None,
    ) -> dict[str, Any]:
        payload = {**target, "clear_first": clear_first}
        if sensitive:
            payload["text_redacted"] = True
        else:
            # Scrubbed whole, then cut: a secret that straddles the limit would survive the other way round.
            preview = scrubber.text(text).text if scrubber is not None else text
            payload["text_preview"] = preview[:preview_chars]
            if len(preview) > preview_chars:
                # So a script exported from the audit log says it types only the start of the text (ACT-31).
                payload["text_truncated"] = True
        return payload

    @staticmethod
    def withhold_text_preview(payload: dict[str, Any]) -> None:
        """For a field not yet known not to be secret: its preview goes, and the mark that the preview was cut."""
        for key in _TEXT_PREVIEW_KEYS:
            payload.pop(key, None)
        payload["text_redacted"] = True

    @staticmethod
    def give_back_text_preview(payload: dict[str, Any], preview: dict[str, Any]) -> None:
        """For a field read and found not secret: the preview withheld until then comes back. A text the caller
        marked sensitive has none to give back, and stays redacted."""
        if not preview:
            return
        payload.pop("text_redacted", None)
        payload.update(preview)

    @staticmethod
    def is_sensitive_field(attributes: Mapping[str, Any]) -> bool:
        """Whether a field's own attributes (type, name, id, autocomplete, placeholder, label) say it is secret."""
        input_type = (attributes.get("type") or "").strip().lower()
        if input_type == "password":
            return True

        autocomplete = (attributes.get("autocomplete") or "").strip().lower()
        if autocomplete in {"current-password", "new-password", "one-time-code"}:
            return True

        haystack = " ".join(str(value or "") for value in attributes.values()).lower()
        return bool(re.search(r"password|passcode|otp|one[- ]time|verification|token|secret|2fa|mfa", haystack))

    async def locator_center(self, locator: Any, *, timeout_ms: float | None = None) -> tuple[float, float] | None:
        try:
            box = await locator.bounding_box(timeout=timeout_ms)
        except Exception:
            return None
        if not box:
            return None
        return (float(box["x"] + box["width"] / 2), float(box["y"] + box["height"] / 2))

    async def move_mouse_human_like(self, session: "BrowserSession", x: float, y: float) -> None:
        start = session.mouse_position
        if start is None:
            start = (
                self.manager.settings.default_viewport_width / 2 + random.randint(-120, 120),
                self.manager.settings.default_viewport_height / 2 + random.randint(-80, 80),
            )
            await session.page.mouse.move(start[0], start[1])
            session.mouse_position = start

        start_x, start_y = start
        control_1 = (
            start_x + (x - start_x) * random.uniform(0.2, 0.4) + random.randint(-80, 80),
            start_y + (y - start_y) * random.uniform(0.1, 0.5) + random.randint(-80, 80),
        )
        control_2 = (
            start_x + (x - start_x) * random.uniform(0.6, 0.85) + random.randint(-60, 60),
            start_y + (y - start_y) * random.uniform(0.5, 0.9) + random.randint(-60, 60),
        )
        steps = random.randint(18, 34)
        for step in range(1, steps + 1):
            t = step / steps
            inv = 1 - t
            px = inv**3 * start_x + 3 * inv * inv * t * control_1[0] + 3 * inv * t * t * control_2[0] + t**3 * x
            py = inv**3 * start_y + 3 * inv * inv * t * control_1[1] + 3 * inv * t * t * control_2[1] + t**3 * y
            step_started = _step_clock()
            await session.page.mouse.move(px, py)
            # The gap between moves is meant to be 4-18 ms. A move already waits
            # for the browser to dispatch it (about a frame, ~17 ms, in Chromium),
            # and sleeping the whole gap on top of that made each step 20-35 ms:
            # a 26-step path took ~670 ms of every click. Sleep only what is left.
            remaining = random.uniform(0.004, 0.018) - (_step_clock() - step_started)
            if remaining > 0:
                await asyncio.sleep(remaining)
        session.mouse_position = (x, y)

    async def click_human_like(self, session: "BrowserSession", x: float, y: float) -> None:
        """Click at (x, y), a little off it as a hand is, but never on another element than the one at (x, y).

        The press used to land up to JITTER_PX from the point each way, on whatever was there: a click aimed at a
        small control pressed its neighbour, which nothing had read (the fast profile's classifier reads the
        element at the point the click names, click_target.py). The comparison runs once the pointer has arrived
        and the pause before the press is over; when the elements differ, or cannot be compared, the pointer
        goes to the point itself.
        """
        jitter_x = x + random.uniform(-JITTER_PX, JITTER_PX)
        jitter_y = y + random.uniform(-JITTER_PX, JITTER_PX)
        await self.move_mouse_human_like(session, jitter_x, jitter_y)
        await self.pause_before_press()
        if not await same_element_at(self.manager.element_ids, session.page, (x, y), (jitter_x, jitter_y)):
            jitter_x, jitter_y = x, y
            await session.page.mouse.move(x, y)
        await self.press_mouse(session, jitter_x, jitter_y)

    @staticmethod
    async def pause_before_press() -> None:
        """The pause between the pointer arriving and the press; any hit test runs after it."""
        await asyncio.sleep(random.uniform(0.03, 0.12))

    async def press_mouse(self, session: "BrowserSession", x: float, y: float) -> None:
        """Press and release at (x, y), where the pointer is. For an action whose target is read, what is under
        the pointer is read first, and a button nobody was asked about is not pressed.
        """
        if press_is_read():
            await refuse_press_not_asked_about(self.manager.element_ids, session.page, x, y, session_id=session.id)
        await session.page.mouse.down()
        await asyncio.sleep(random.uniform(0.02, 0.08))
        await session.page.mouse.up()
        session.mouse_position = (x, y)

    async def click_locator_human_like(
        self,
        session: "BrowserSession",
        locator: Any,
        coords: tuple[float, float],
        *,
        timeout_ms: float | None = None,
        budget: _Budget | None = None,
    ) -> None:
        """Click `locator` along a human-like path, but only if it is what the pointer hits.

        Pressing the mouse at an element's centre sends the click to whatever is
        on top there: a transparent element over a "Save draft" button took the
        click, while the approval and the audit trail named the button. Playwright's
        own checks run first (visible, stable, enabled, receiving events at its
        click point, frames included, but a frame CSS transforms, which Playwright
        does not look through), and the point is checked again once the
        pointer has arrived and the pause before the press is over, so a cover
        put up during the move or the pause is caught as well. Enabled is asked
        again with it: the move and the pause take about half a second, and a
        button the page turned off in that time got a mouse down that the browser
        gives to nobody, under an action that ended as done. When that last
        check fails the click is one Playwright places (click_as_playwright_places_it):
        its own `click()`, or for an action whose target is read its hover and the
        controller's press where that put the pointer. Either clicks the element if
        it can and otherwise refuses, and both wait for a target that is not
        enabled. The controller's press does not click what covers the element;
        Playwright's own `click()` does where the cover is over a frame CSS
        transforms (docs/production-hardening.md has that limit).

        The click has one budget for all its waits, as Playwright's own click has
        one timeout: ``budget`` where the caller began it (a step of the same
        click came first), else one of ``timeout_ms``. The placed click is given
        what is left of it. It had a whole budget of its own, so a target that
        kept the checks waiting and then the placing held its session for two.
        """
        budget = budget or self._budget(timeout_ms)
        await self._checks_of_a_click(locator, budget)
        x = coords[0] + random.uniform(-JITTER_PX, JITTER_PX)
        y = coords[1] + random.uniform(-JITTER_PX, JITTER_PX)
        await self.move_mouse_human_like(session, x, y)
        await self.pause_before_press()
        if await self._receives_pointer_at(
            locator, x, y, timeout_ms=budget.left_ms(POINTER_CHECK_TIMEOUT_MS)
        ) and await self._is_enabled_now(locator, budget.left_ms(POINTER_CHECK_TIMEOUT_MS)):
            await self.press_mouse(session, x, y)
        else:
            await self.click_as_playwright_places_it(session, locator, budget=budget)

    @staticmethod
    async def _is_enabled_now(locator: Any, timeout_ms: float) -> bool:
        """Whether the target is enabled as its press is about to be made. What cannot be told is a no."""
        try:
            return bool(await locator.is_enabled(timeout=timeout_ms))
        except Exception:
            return False

    def _budget(self, timeout_ms: float | None) -> _Budget:
        """A click's budget: the caller's, or the one Playwright gives an action of this session (ACTION_TIMEOUT_MS)."""
        return _Budget(self.manager.settings.action_timeout_ms if timeout_ms is None else timeout_ms)

    @staticmethod
    async def _scroll_into_view(locator: Any, budget: _Budget) -> None:
        """Scroll a click's target into view, within the click's budget.

        Playwright's scroll finds one element and waits for it to stand still. When the page replaces that
        element meanwhile it ends at once ("Element is not attached to the DOM"), where Playwright's click
        looks the element up again. A disabled button that a form draws again many times a second ended the
        click there, one step before the checks that wait for such a button (_checks_of_a_click). The scroll
        is made again while the budget lasts. Any other failure is the click's.
        """
        while True:
            try:
                await locator.scroll_into_view_if_needed(timeout=budget.left_ms())
                return
            except PlaywrightTimeoutError:
                raise
            except PlaywrightError as error:
                if budget.spent or _NOT_ATTACHED not in error_text(error):
                    raise
            await asyncio.sleep(ENABLED_POLL_SECONDS)

    @staticmethod
    async def _checks_of_a_click(locator: Any, budget: _Budget) -> None:
        """Playwright's checks of a click on ``locator``, with nothing pressed.

        Visible, enabled, stable, and the element is what the pointer finds at its click point, frames
        included. These were a trial click, and a trial click presses: Playwright sends the mouse down and up
        for real and cancels the events with a listener in the target's own frame. Over a frame that the target
        holds they go to the frame's document, which that listener never sees: a click that named a frame or
        its wrapper pressed the frame's button once before its own press (a "Pay" pressed twice), and for an
        action whose target is read, before the read. A click listener a site keeps on window, in the capture
        phase, saw them as well.

        A trial hover makes the same checks but one, and sends the pointer's move alone. That one, enabled, is
        waited for first, as a click waits for it: by looking again, and each look finds the element the
        selector names then. A wait on the one element found first ended at once when a page drew its disabled
        button again ("Element is not attached to the DOM"), where a click had waited for the new one. The two
        share the click's budget.
        """
        while not await locator.is_enabled(timeout=budget.left_ms()):
            await asyncio.sleep(ENABLED_POLL_SECONDS)
            if budget.spent:
                raise PlaywrightTimeoutError(
                    f"Timeout {budget.timeout_ms:g}ms exceeded: the click's target is there and is not enabled."
                )
        await locator.hover(trial=True, timeout=budget.left_ms())

    async def click_as_playwright_places_it(
        self,
        session: "BrowserSession",
        locator: Any,
        *,
        budget: _Budget | None = None,
    ) -> None:
        """Playwright's own click on ``locator``, for a target the human-like press could not take.

        Playwright picks the point of its click itself, the middle of the first piece of the element that the
        viewport shows, and presses in the same step: there is no moment between for the read a press takes
        (press_mouse), and no way to ask it for the point. Read at the middle of the element's box instead, a
        wrapper wider than the viewport, or one in two pieces over two lines, was read at a point the click never
        touched, and the button under Playwright's point was pressed unread.

        So for an action whose target is read the step is taken apart, and only the placing is Playwright's. Its
        checks run (_checks_of_a_click), its hover puts the pointer at the point its click would have picked, given to
        it as a position so that the controller knows where that is, and the press is the controller's own: read
        where it goes down, as every other press is. Whether the target takes the pointer at that point is
        Playwright's check, made by its hover, and then the controller's own (_receives_pointer_at), asked with
        the pointer there. Playwright's is not enough: it does not look through a frame that stands in
        anything CSS transforms (it puts the pointer where it is told and watches for the event in the target's
        own document, so one that goes to the page over the frame, or into a frame that lies over the target,
        never comes, and it calls that done), and between its hover and the press a page can take the target
        away. An element made invisible keeps its box, which was all that was asked for then, and the press
        went to what was behind it. The steps share the click's budget (_Budget), as the steps of Playwright's
        own click share its timeout: ``budget`` where the click began before this (a human-like try, the scroll
        into view), else the session's own (ACTION_TIMEOUT_MS). Playwright's own click is given what is left of it.
        """
        budget = budget or self._budget(None)
        if not press_is_read():
            await locator.click(timeout=budget.left_ms())
            return
        await self._checks_of_a_click(locator, budget)
        seen, place = await self._place_of(locator, budget)
        if place["framed"] and seen and not place["boxless"]:
            # What the frame shows of the target is not what the screen shows of it: asked again with its size.
            size = await session.page.locator(":root").evaluate(
                "() => [innerWidth, innerHeight]", timeout=budget.left_ms()
            )
            place = await locator.evaluate(_PRESS_PLACE_SCRIPT, [seen, size], timeout=budget.left_ms())
        if place["boxless"]:
            # Shown when the checks passed a moment ago, or they had not: one with no box now, and not for its
            # children's sake, was hidden since. It has nothing inside it to name.
            raise _target_has_no_box() if place["contents"] else _target_left_the_pointer()
        position = {"x": float(place["x"]), "y": float(place["y"])}
        await locator.hover(position=position, timeout=budget.left_ms())
        # In a frame the target's document is asked where it sees the pointer's move below arrive, and the
        # question is asked at that point (_POINTER_WATCH_SCRIPT). (x, y) is counted from Playwright's box, which in
        # a frame of another process with a border or padding is up and left of the element by that much: the
        # pointer went there, and the question, carried into the frame by that same box, was asked beside the
        # pointer. The target is looked for once, for its box and for the watch, and both handles are given back
        # once the click has pressed or ended: a handle is the page's to keep until then. The watch is set up once
        # the target has a box: set up for a click that ends here, it listened on until the frame's next move.
        target = watch = None
        try:
            if place["framed"]:
                target = await locator.element_handle(timeout=budget.left_ms())
                box = await target.bounding_box()
                if box:
                    watch = await target.evaluate_handle(_POINTER_WATCH_SCRIPT)
            else:
                box = await locator.bounding_box(timeout=budget.left_ms())
            if not box:
                raise _target_left_the_pointer()
            # Where Playwright puts a position: the corner of the element's box, its border, the offset.
            x = float(box["x"] + place["border"][0] + position["x"])
            y = float(box["y"] + place["border"][1] + position["y"])
            await session.page.mouse.move(x, y)
            asked_within = budget.left_ms(POINTER_CHECK_TIMEOUT_MS)
            # A question the page does not answer within its bound raises its timeout (_receives_pointer_at), and
            # the click ends as that timeout, as at any step before this one: a no is the page's answer.
            if not await self._receives_pointer_at(
                locator, x, y, placed=True, watch=watch, placed_by=box, timeout_ms=asked_within
            ):
                raise _target_not_under_the_pointer()
            await self.press_mouse(session, x, y)
        finally:
            await self._give_back(watch, target)

    @staticmethod
    async def _give_back(watch: Any, target: Any) -> None:
        """End a placed click's watch, where its record was not read, and give the watch and the target back.

        Each on its own: a cancellation at one (it is no Exception) skipped the next, which the page then kept for
        good. It is raised once all are done. Without the stop, a click whose record was never read (no move came
        to the target's document, and the question ended before it read the record) left the watch's listeners on
        the frame's window until its next move.
        """
        steps: list[Any] = []
        if watch is not None:
            steps += [lambda: watch.evaluate(_POINTER_WATCH_STOP_SCRIPT), watch.dispose]
        if target is not None:
            steps.append(target.dispose)
        await _each_on_its_own(steps)

    @staticmethod
    async def _place_of(locator: Any, budget: _Budget) -> tuple[Any, dict[str, Any]]:
        """The box Playwright finds for the target, and the place to hover at, counted as Playwright counts one.

        The box is read a round trip before the script that picks the place. A place counted from it
        (``counted``: an element of several boxes in something turned, whose box is not the script's size) is
        right only where that box still stood when the script ran. A target the page moved and resized in that
        round trip has boxes of two sizes as well, and was aimed at off itself: Playwright's hover kept the offset
        through every retry and waited the click's time out, where its own click pressed it. So the box is read
        again, and the place counted again where it changed, PLACE_RECOUNTS times at most: a target that changes
        at every read is hovered at the last place counted.
        """
        seen = await locator.bounding_box(timeout=budget.left_ms())
        place = await locator.evaluate(_PRESS_PLACE_SCRIPT, [seen, None], timeout=budget.left_ms())
        for _ in range(PLACE_RECOUNTS):
            if not place["counted"]:
                break
            again = await locator.bounding_box(timeout=budget.left_ms())
            if again == seen:
                break
            seen = again
            place = await locator.evaluate(_PRESS_PLACE_SCRIPT, [seen, None], timeout=budget.left_ms())
        return seen, place

    @staticmethod
    async def _receives_pointer_at(
        locator: Any,
        x: float,
        y: float,
        *,
        placed: bool = False,
        watch: Any = None,
        placed_by: dict[str, float] | None = None,
        timeout_ms: float = POINTER_CHECK_TIMEOUT_MS,
    ) -> bool:
        """Whether the pointer at (x, y) finds the element ``locator`` names, through the frames it is in.

        A target in the main frame is asked in one message, as before. One inside a frame is asked in its own
        document, and then each frame element on the way up is asked in the document that holds it: the frame
        must be what the pointer finds there. A press at the middle of a link's box, where the link was wider
        than its frame, landed on the page beside the frame, and the click ended as done. Anything that cannot
        be told is a no, and the click is then one Playwright places.

        ``placed``: (x, y) is where Playwright's hover has put the pointer, and a no ends the click. There a
        target POINTER_CHECK_FRAMES frames down or deeper is a yes: the documents asked said so, the ones above
        them are Playwright's hover's to check, and a no would end a click that Playwright's own makes.

        ``watch``: the target is in a frame, and this is the record of where its document saw the pointer arrive
        (_POINTER_WATCH_SCRIPT), read here in the target's own document, and the caller's to give back. The target
        is then asked at that point, a point of its own document, and not at (x, y) carried by its two boxes.
        Playwright's box of an element in a frame of another process leaves out the frame's border and padding,
        so with either the carried point was that far down and right of the pointer: a button whose part on the
        screen ended within that much of the pointer ended the click as target_moved, where Playwright's own click
        pressed it, and what the page put under the pointer once Playwright's hover had passed (a row's "Delete")
        was pressed where it did not reach that far. No move seen is a no, the pointer being over another
        document (or the page stopped both moves on the window before the watch heard them), unless what lies at
        the carried point is a frame inside the target (a video in a card), which Playwright's own check takes for
        its target. A document that runs no script can say nothing, and its target is asked by the two boxes. The
        frames on the way up are asked as before.

        ``placed_by``: the target's box the click was placed by, with the watch. The record is of a point of the
        target's document, and a page that moves the frame once the pointer has arrived (on that move) leaves the
        pointer, which does not move, over another point of it: asked at the record, the question found the card,
        and the press went down on what the move had put under the pointer, where nothing reads it in a frame of
        another origin. So the record is taken only where the target's box is still that one, its corner and its
        size, to PLACED_BOX_MOVED_PX each way; else no (target_moved). A frame of the page's own process scaled
        about the target's corner keeps the corner, and the press went down where the scale had put the pointer.
        The box read again is the one the two boxes carry the point by where there is no record, and they carry
        it as the frame is now. Not seen: Playwright counts a box in a frame of another process as if the frame
        were not turned or scaled and had no border or padding, so a page that turns such a frame (a half turn),
        scales it about its top-left corner or grows its padding inside the same border box as the pointer arrives
        keeps the box, and the press goes down where the change put the pointer. That is a page that changes what
        is under the pointer, which click_target.py's "Not caught" lists; a guard of every frame's geometry read in
        the page's own world, which such a page can answer for itself, would not stop it.

        ``timeout_ms`` bounds the wait for the element it asks about: POINTER_CHECK_TIMEOUT_MS, or what the
        click has left where that is less. The first message had no bound given, so it waited by Playwright's
        default, a whole ACTION_TIMEOUT_MS more, for a target that left once the click's checks had passed. Where
        the click was placed, a wait that runs past it raises its timeout: the page did not answer, which is no
        no. Taken for one, it ended a click as target_moved where the page was busy, with the click's time not
        spent, and every step before the question ends a click as the timeout it is.
        """
        handles: list[Any] = []
        try:
            if watch is None:
                found = await locator.evaluate(
                    _RECEIVES_POINTER_SCRIPT, [x, y, None, False, placed], timeout=timeout_ms
                )
                if found != "framed":
                    return found is True
            # Each handle is noted where it is taken, to be given back below: noted at the top of a turn, the
            # one taken on the last turn of a walk that ran out of frames was kept by the page for good.
            node = await locator.element_handle(timeout=timeout_ms)
            handles.append(node)
            # Where the target's document saw the pointer arrive: a point there, None for no move, False for a
            # document that runs no script, and for no watch (the two boxes carry the point, as before).
            at = await node.evaluate(_POINTER_SEEN_SCRIPT, watch) if watch is not None else False
            only_frames = at is None
            # The element itself, before a human-like press, is asked as well that the pointer is over it, and
            # where the click was placed, as the button or the link it is in. Neither is asked of a frame.
            arrived, wide = not placed, placed
            for _ in range(POINTER_CHECK_FRAMES):
                box = await node.bounding_box()
                if at and not _box_kept(box, placed_by):
                    return False
                asked = [*(at or (x, y)), True if at else box, arrived, wide]
                if only_frames:
                    asked.append(True)
                if not box or await node.evaluate(_RECEIVES_POINTER_SCRIPT, asked) is not True:
                    return False
                at = only_frames = arrived = wide = False
                frame = await node.owner_frame()
                if frame is None or frame.parent_frame is None:
                    return True
                node = await frame.frame_element()
                handles.append(node)
            return placed
        except PlaywrightTimeoutError:
            if placed:
                raise
            return False
        except Exception:
            return False
        finally:
            await _each_on_its_own([handle.dispose for handle in handles])

    async def focus_locator(self, session: "BrowserSession", locator: Any) -> None:
        coords = await self.locator_center(locator)
        if coords is None:
            await self.click_as_playwright_places_it(session, locator)
        else:
            await self.click_locator_human_like(session, locator, coords, timeout_ms=FOCUS_CLICK_TIMEOUT_MS)
        await asyncio.sleep(0.05 + random.random() * 0.1)

    async def focus_verified(self, session: "BrowserSession", locator: Any, *, action: str) -> None:
        """Put keyboard focus in `locator` and prove it is there before anything is typed.

        Keystrokes go to whatever has focus. A field that hands focus on to
        another one, or a click that never reached the field, sent the text —
        sometimes a password — somewhere the caller did not name.
        """
        try:
            await self.focus_locator(session, locator)
        except PlaywrightError:
            # Covered or not clickable: focus it without clicking anything.
            pass
        except BrowserActionError as exc:
            # So too where the controller's own press refused the click Playwright placed: nothing was pressed.
            if exc.code != TARGET_MOVED:
                raise
        if await self._has_focus(locator):
            return
        await locator.focus()
        if await self._has_focus(locator):
            return
        raise BrowserActionError(
            "Keyboard focus would not stay in the target field, so nothing was typed.",
            action=action,
            code="focus_lost",
            retryable=True,
        )

    @staticmethod
    async def _has_focus(locator: Any) -> bool:
        try:
            return bool(await locator.evaluate(ELEMENT_HAS_FOCUS_SCRIPT))
        except Exception:
            return False

    async def type_text_human_like(self, page: "Page", text: str) -> None:
        delays_ms: list[int] = []
        for index in range(len(text)):
            delay_ms = random.randint(
                self.manager.settings.human_typing_min_delay_ms,
                self.manager.settings.human_typing_max_delay_ms,
            )
            if index > 0 and index % random.randint(6, 12) == 0:
                delay_ms += random.randint(180, 600)
            delays_ms.append(delay_ms)
        # Scale every delay down together when the pauses alone would overrun the budget. Each keystroke is a round
        # trip to a browser node, outside the pauses, so the deadline is what bounds the total: once it passes, the
        # rest is inserted as plain text, with each newline, carriage return or tab in it still pressed as Enter or
        # Tab (_insert_rest), all within one shared cutoff.
        scale = min(1.0, HUMAN_TYPING_BUDGET_SECONDS * 1000 / max(sum(delays_ms), 1))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HUMAN_TYPING_BUDGET_SECONDS
        for index, (char, delay_ms) in enumerate(zip(text, delays_ms)):
            if loop.time() >= deadline:
                await self._insert_rest(page, text[index:], deadline)
                return
            timeout = max(deadline - loop.time(), HUMAN_TYPING_STEP_FLOOR_SECONDS)
            await self._typing_step(page.keyboard.type(char), timeout)
            await asyncio.sleep(min(delay_ms * scale / 1000, max(deadline - loop.time(), 0)))

    async def _insert_rest(self, page: "Page", rest: str, deadline: float) -> None:
        """The text past the deadline: inserted as plain text, but newline and tab are still pressed as keys.

        insert_text of a newline into an <input> drops it, so a long text ending in one would not submit the form
        as the same text typed in time does (keyboard.type presses Enter and Tab for them).
        """
        # One cutoff for every remaining step: a floor per step would let a text with many newlines run far past it.
        loop = asyncio.get_running_loop()
        cutoff = max(deadline, loop.time()) + HUMAN_TYPING_STEP_FLOOR_SECONDS
        for part in _CONTROL_KEY_PATTERN.split(rest):
            if not part:
                continue
            left = cutoff - loop.time()
            if left <= 0:
                raise self._typing_timeout()
            if part in _CONTROL_KEYS:
                await self._typing_step(page.keyboard.press(_CONTROL_KEYS[part]), left)
            else:
                await self._typing_step(page.keyboard.insert_text(part), left)

    @staticmethod
    def _typing_timeout() -> BrowserActionError:
        return BrowserActionError(
            "Typing stopped: the page did not acknowledge a keystroke in time. Part of the text may have been "
            "entered; check the field before typing again.",
            code="typing_timeout",
            action="type",
            status_code=504,
            # Not retryable (D3a-7): typing the text again could enter the part already in the field twice.
            retryable=False,
        )

    @classmethod
    async def _typing_step(cls, step: Any, timeout: float) -> None:
        """One keystroke or the insert, bounded: a page that never acknowledges input would hold the session lock."""
        try:
            await asyncio.wait_for(step, timeout)
        except TimeoutError:
            raise cls._typing_timeout() from None

    async def _find_one_time_code_field(self, page: "Page") -> tuple[Any, str] | None:
        for selector in _ONE_TIME_CODE_FIELD_SELECTORS:
            try:
                candidates = await page.locator(selector).all()
            except PlaywrightError:
                continue
            for candidate in candidates[:_MAX_CODE_FIELD_CANDIDATES]:
                try:
                    if not await candidate.is_visible():
                        continue
                    attributes = await candidate.evaluate(_FIELD_ATTRIBUTES_SCRIPT)
                except PlaywrightError:
                    continue  # detached while we looked; the next candidate may still do
                if is_one_time_code_field(attributes):
                    return candidate, selector
        return None

    async def maybe_handle_totp(self, session: "BrowserSession") -> dict[str, Any] | None:
        if not session.totp_secret:
            return None
        # A live code is typed into whatever "code" field is visible, so the
        # page has to be one the caller named: otherwise any site the agent is
        # sent to can show an input named "code" and read a valid second factor.
        # Nor a refused page on a named host: a refused host's redirect chose its URL.
        if not self._may_fill_totp(session):
            return None
        if pyotp is None:
            raise BrowserActionError(
                "TOTP support is not installed in this controller runtime",
                action="totp_fill",
                code="totp_unavailable",
                retryable=False,
                details={"url": self.manager._shown_url(session.page)},
            )
        located = await self._find_one_time_code_field(session.page)
        if located is None:
            return None

        locator, selector = located
        # Finding the field awaited the page, which may have navigated since.
        if not self._may_fill_totp(session):
            return None
        code = pyotp.TOTP(session.totp_secret).now()
        await self.focus_verified(session, locator, action="totp_fill")
        # So did the focus click: the pointer's moves, the pause and the press, whose navigation a click waits for.
        if not self._may_fill_totp(session):
            return None
        try:
            await locator.fill("")
        except Exception:
            await session.page.keyboard.press("Control+a")
            await session.page.keyboard.press("Delete")
        # And so did the clear: a field whose page an input or a key handler sends elsewhere as it is emptied.
        if not self._may_fill_totp(session):
            return None
        await self.type_text_human_like(session.page, code)
        # Enter submits the form the code went into. This used to click the
        # first submit button anywhere on the page — on a checkout page that
        # was "Place order", clicked with no approval anywhere in this path.
        await locator.press("Enter")
        await self.manager._settle(session.page)
        return {"selector": selector, "code_length": len(code)}

    def _may_fill_totp(self, session: "BrowserSession") -> bool:
        return not self.manager._page_refused(session.page) and self._totp_host_allowed(session)

    @staticmethod
    def _totp_host_allowed(session: "BrowserSession") -> bool:
        parsed = urlparse(session.page.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        return totp_host_allowed(parsed.hostname, session.totp_hosts)

    def approval_kind_for_decision(self, decision: BrowserActionDecision) -> ApprovalKind | None:
        if decision.action == "upload":
            return "upload" if self.manager.settings.require_approval_for_uploads else None
        if decision.risk_category in {"post", "payment", "account_change", "destructive"}:
            return decision.risk_category
        return None

    @classmethod
    def governed_approval_kind_for_decision(cls, decision: BrowserActionDecision) -> ApprovalKind | None:
        # risk_category is chosen by whoever wrote the decision, often a model
        # reading the page it is acting on. Only an action that cannot change
        # anything may skip governed approval by calling itself "read"; a
        # click, type, press, select or upload labelled "read" is still a write.
        if decision.risk_category == "read" and (
            decision.action == "done" or cls.action_class(decision.action) == "read"
        ):
            return None
        if decision.action == "upload" or decision.risk_category == "upload":
            return "upload"
        if decision.risk_category in {"post", "payment", "account_change", "destructive"}:
            return decision.risk_category
        return "write"

    @staticmethod
    def action_class(action_name: str) -> str:
        return "read" if action_name in READ_ONLY_ACTIONS else "write"

    @staticmethod
    def action_verification(
        action_name: str,
        target: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]:
        signals: list[str] = []
        if before.get("url") != after.get("url"):
            signals.append("url_changed")
        if before.get("title") != after.get("title"):
            signals.append("title_changed")
        if before.get("active_element") != after.get("active_element"):
            signals.append("active_element_changed")
        if before.get("text_excerpt") != after.get("text_excerpt"):
            signals.append("text_excerpt_changed")

        before_counts = (before.get("dom_outline") or {}).get("counts") or {}
        after_counts = (after.get("dom_outline") or {}).get("counts") or {}
        if before_counts != after_counts:
            signals.append("dom_counts_changed")

        before_accessibility = (before.get("accessibility_outline") or {}).get("focused")
        after_accessibility = (after.get("accessibility_outline") or {}).get("focused")
        if before_accessibility != after_accessibility:
            signals.append("accessibility_focus_changed")

        before_tabs, after_tabs = _tab_count(before), _tab_count(after)
        if before_tabs is not None and after_tabs is not None and before_tabs != after_tabs:
            signals.append("tabs_changed")

        interacted_element = target.get("element_id")
        selector = target.get("selector")
        interactables = after.get("interactables") or []
        target_seen_after = None
        if interacted_element:
            target_seen_after = any(item.get("element_id") == interacted_element for item in interactables)
        elif selector:
            target_seen_after = any(item.get("selector_hint") == selector for item in interactables)

        if target_seen_after is True:
            signals.append("target_still_visible")
        elif target_seen_after is False:
            signals.append("target_no_longer_visible")

        delivery = target.get("delivery")
        if delivery is not None:
            signals.append(f"upload_{delivery['outcome']}")

        verified: bool | None = bool(signals)
        if action_name in {"navigate", "go_back", "go_forward"}:
            verified = "url_changed" in signals or "title_changed" in signals
        elif action_name in {
            "click",
            "press",
            "scroll",
        }:
            verified = bool(
                {
                    "url_changed",
                    "title_changed",
                    "active_element_changed",
                    "text_excerpt_changed",
                    "accessibility_focus_changed",
                }
                & set(signals)
            ) or (action_name != "scroll" and "tabs_changed" in signals)
        elif action_name == "hover":
            verified = (
                bool({"active_element_changed", "text_excerpt_changed", "accessibility_focus_changed"} & set(signals))
                or target_seen_after is not None
            )
        elif action_name in {"type", "select_option"}:
            verified = bool(
                {"active_element_changed", "text_excerpt_changed", "accessibility_focus_changed"} & set(signals)
            )
        elif action_name in {"wait", "reload"}:
            # Neither is verified either way (ACT-30): a wait has nothing to verify, and a reload of a page that did
            # not change leaves nothing to compare. They read true before, whatever the page did.
            verified = None
        elif action_name == "upload":
            # By element_id, whether the registered input holds the file; by selector, Playwright's word.
            verified = delivery is None or delivery["outcome"] == "confirmed"

        return {
            "verified": verified,
            "signals": signals,
            "target_seen_after": target_seen_after,
        }

    @staticmethod
    def new_tab(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any] | None:
        """The tab an action opened, for its result (NAV-08): the newest, which the tab list puts last.

        Only while it is not the active tab, as NEW_TAB_MESSAGE says it is not.
        """
        before_tabs, tabs = _tab_count(before), after.get("tabs")
        if before_tabs is None or not isinstance(tabs, list) or len(tabs) <= before_tabs or tabs[-1].get("active"):
            return None
        return {"index": tabs[-1]["index"], "message": NEW_TAB_MESSAGE}

    @staticmethod
    def resolve_target(
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        """The target an action records. One with an element_id is acted on through the page's isolated world
        (app/browser/element_target.py); the selector_hint recorded beside it is never matched against the page."""
        element_id = target_element_id(selector=selector, element_id=element_id)
        if element_id:
            return {"mode": "selector", "element_id": element_id, "selector": selector_hint(element_id)}
        if selector:
            return {"mode": "selector", "selector": selector}
        if x is not None and y is not None:
            return {"mode": "coordinates", "x": x, "y": y}
        raise ValueError("Provide selector, element_id, or x+y coordinates")
