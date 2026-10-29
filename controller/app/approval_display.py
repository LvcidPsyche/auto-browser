"""What an operator, an auditor and a notification are shown of an approval: written by the server.

The reason of an action approval is the agent's own words; what is here comes
from the record itself, with the characters that could make it read as
something else written as their code points.
"""

from __future__ import annotations

import json
import math
import unicodedata
from decimal import Decimal
from typing import Any

from .models import ApprovalRecord, BrowserActionDecision

# Characters of one string field a short account of an approval shows before it says how many it
# leaves out: the webhook's and the SSE event's summary, and the dashboard's confirm dialog. The
# queue row cuts nothing (every field is bounded: text at 5000 characters, selectors and URLs at 2000).
APPROVAL_SUMMARY_CUT = 200
# What an operator is shown of an action approval, in order: a field, and the actions it is shown
# for (None: any action that sets it). One list for approval_summary and the dashboard's
# describeAction (injected into the page), and a test checks it covers every field an approval
# binds (_actions_match). The target leads, the element id before the selector: when both are
# set, the element id is what runs (BrowserActionService.resolve_target).
APPROVAL_FIELDS: tuple[tuple[str, tuple[str, ...] | None], ...] = (
    ("element_id", None),
    ("selector", None),
    ("x", None),
    ("y", None),
    ("url", None),
    ("file_path", None),
    ("key", None),
    ("value", None),
    ("label", None),
    ("index", None),
    ("text", None),
    ("clear_first", ("type",)),
    ("delta_x", ("scroll",)),
    ("delta_y", ("scroll",)),
    ("wait_ms", ("wait",)),
    ("recipient", None),
    ("platform", None),
    ("username", None),
)
# What each action reads of those fields (BrowserActionService._dispatch_decision passes these on, and
# a test checks it): a field shown for an action that does not read it is marked "(not used by click)".
# Any other action (reload, go_back, ...) reads none.
ACTION_READS: dict[str, tuple[str, ...]] = {
    "click": ("element_id", "selector", "x", "y"),
    "hover": ("element_id", "selector", "x", "y"),
    "type": ("element_id", "selector", "text", "clear_first"),
    "select_option": ("element_id", "selector", "value", "label", "index"),
    "upload": ("element_id", "selector", "file_path"),
    "navigate": ("url",),
    "press": ("key",),
    "scroll": ("delta_x", "delta_y"),
    "wait": ("wait_ms",),
}
# Of the fields an action reads, those another field set beside them overrides, the first that runs
# first: the element id over the selector over the coordinates (resolve_target), and an option's index
# over its value over its label (select_option). Marked "(not used: element_id runs)".
OVERRIDDEN_BY: dict[str, tuple[str, ...]] = {
    "selector": ("element_id",),
    "x": ("element_id", "selector"),
    "y": ("element_id", "selector"),
    "value": ("index",),
    "label": ("index", "value"),
}
# Bound fields the list leaves out, shown otherwise: the verb leads the account, the sensitive flag
# decides how the text shows, and the risk category decides the approval's kind, shown beside it.
APPROVAL_FIELDS_SHOWN_OTHERWISE = frozenset({"action", "sensitive", "risk_category"})
# What an approval does not bind (ApprovalStore._actions_match): the agent's account of the action.
UNBOUND_FIELDS = frozenset({"reason", "confidence"})
# Written after a field the action does not read, after one another field overrides, and for a
# sensitive action's text. The agent sets sensitive on any field, a password or not, so the label
# says who hid the text.
NOT_USED_BY = "(not used by {action})"
NOT_USED_FOR = "(not used: {field} runs)"
SENSITIVE_TEXT_HIDDEN = "text hidden at the agent's request (sensitive)"
# Written after a select_option value that runs: it goes to Playwright as valueOrLabel (element_target
# matches the same way), so it picks the first option whose value or whose label it is.
VALUE_OR_LABEL = "(matches an option's value or label)"
# What the dashboard renders the queue with (routes/extensions/dashboard_html.py).
APPROVAL_DISPLAY = {
    "fields": APPROVAL_FIELDS,
    "reads": ACTION_READS,
    "overriddenBy": OVERRIDDEN_BY,
    "cut": APPROVAL_SUMMARY_CUT,
    "notUsedBy": NOT_USED_BY,
    "notUsedFor": NOT_USED_FOR,
    "sensitiveTextHidden": SENSITIVE_TEXT_HIDDEN,
    "valueOrLabel": VALUE_OR_LABEL,
}


# Characters that hide or reorder what follows them: format characters (bidi controls, zero-width
# marks), controls, and the line and paragraph separators (each ends a // comment in JavaScript).
_HIDDEN_CATEGORIES = frozenset({"Cf", "Cc", "Zl", "Zp"})
# Default_Ignorable_Code_Point (variation selectors, Hangul fillers, ...), which unicodedata does not
# expose: the ranges of DerivedCoreProperties.txt as of Unicode 17.0, as node 22 (ICU 78) reports them
# for the dashboard's \p{Default_Ignorable_Code_Point}.
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)

# Format characters (Cf) as of Unicode 17.0 (DerivedGeneralCategory.txt), as node 22 (ICU 78) reports
# them for the dashboard's \p{Cf}; joined with what the running unicodedata says. The shipped image's
# Python (3.11, Unicode 14.0) reads U+13439..U+1343F as unassigned, so without the table the server
# showed what the dashboard escaped. Unassigned code points (Cn) stay as they are.
_FORMAT_CHARACTERS = (
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)


def _in(code: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(low <= code <= high for low, high in ranges)


def _shown(char: str) -> str:
    if char == "\n":
        return "\\n"
    code = ord(char)
    hidden = (
        unicodedata.category(char) in _HIDDEN_CATEGORIES
        or _in(code, _FORMAT_CHARACTERS)
        or _in(code, _DEFAULT_IGNORABLE)
    )
    return f"[U+{code:04X}]" if hidden else char


def shown_code_points(text: str) -> str:
    """``text`` with each character that would hide or reorder what follows written as its code point.

    They are invisible, and a bidi override reorders what follows it: in the
    text an operator approves, they could make it read as something else. A
    newline shows as \\n. Letters stay as they are, those of other scripts too:
    a homoglyph or a right-to-left word still reads as itself (a limit).
    """
    return "".join(_shown(char) for char in text)


def approval_tool(approval: ApprovalRecord) -> str | None:
    """The governed tool a tool-call approval is for, as its stand-in names it; None for an action approval."""
    if approval.origin != "tool_call":
        return None
    return (approval.action.text or "").split(" sha256:", 1)[0] or None


def approval_provenance(approval: ApprovalRecord) -> dict[str, str]:
    """Who built an approval, for its receipts and audit entries: the origin, and a tool call's tool.

    A tool call's stand-in is a request_human_takeover, and the trail recorded
    only that: an auditor read "request_human_takeover approved" where an
    operator had approved browser.eval_js.
    """
    tool = approval_tool(approval)
    return {"origin": approval.origin} if tool is None else {"origin": approval.origin, "tool": tool}


def approval_summary(approval: ApprovalRecord) -> str:
    """What an approval is for, written by the server: the tool for a tool call, else the action and its fields.

    The reason is the agent's own words for an action approval, and a
    notification that carried only the reason read the same for a coordinate
    click as for the tool call whose reason it copied.
    """
    tool = approval_tool(approval)
    if tool is not None:
        return shown_code_points(f"tool call {tool}")
    return describe_action(approval.action, cut=APPROVAL_SUMMARY_CUT)


def describe_action(action: BrowserActionDecision, *, cut: int | None) -> str:
    """The action, field by field from APPROVAL_FIELDS, as the dashboard's describeAction writes it.

    Each value as JSON, so a string shows its quotes and a newline in it shows
    as an escape; a string longer than ``cut`` characters says how many it leaves out.
    """
    fields = action.model_dump()
    parts = [action.action]
    for name, actions in APPROVAL_FIELDS:
        value = fields[name]
        # An empty string is set: only a value and a label can be one, and either runs (select_option).
        if (actions is not None and action.action not in actions) or value is None:
            continue
        part = SENSITIVE_TEXT_HIDDEN if name == "text" and action.sensitive else f"{name}={_shown_value(value, cut)}"
        note = unused_note(action.action, name, fields)
        if note is None and action.action == "select_option" and name == "value":
            note = VALUE_OR_LABEL
        parts.append(f"{part} {note}" if note else part)
    return shown_code_points(" · ".join(parts))


def unused_note(action: str, name: str, fields: dict[str, Any]) -> str | None:
    """Why the field ``name``, set, does not act: the action does not read it, or a field set beside it runs."""
    reads = ACTION_READS.get(action, ())
    if name not in reads:
        return NOT_USED_BY.format(action=action)
    for other in OVERRIDDEN_BY.get(name, ()):
        if other in reads and fields[other] is not None:
            return NOT_USED_FOR.format(field=other)
    return None


def js_number(value: float) -> str:
    """``value`` as JavaScript writes a number (ECMAScript Number::toString), as the queue row shows it.

    The same shortest digits as Python's repr, laid out JavaScript's way: no
    exponent from 1e-6 up to 1e21, and a bare one past that (1e-7, 1e+21);
    Infinity, -Infinity and NaN as String(n) writes them.
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    _, digit_tuple, exponent = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(map(str, digit_tuple))
    count, point = len(digits), int(exponent) + len(digits)
    if count <= point <= 21:
        shown = digits + "0" * (point - count)
    elif 0 < point <= 21:
        shown = f"{digits[:point]}.{digits[point:]}"
    elif -6 < point <= 0:
        shown = f"0.{'0' * -point}{digits}"
    else:
        mantissa = digits if count == 1 else f"{digits[0]}.{digits[1:]}"
        shown = f"{mantissa}e{'+' if point > 0 else '-'}{abs(point - 1)}"
    return f"-{shown}" if value < 0 else shown


def _as_double(value: int | float) -> float:
    """``value`` as JSON.parse reads it: the nearest double, or Infinity for an integer past the largest one."""
    try:
        return float(value)
    except OverflowError:
        # Not copysign(inf, value): that converts value to a float as well, and raises again.
        return math.inf if value > 0 else -math.inf


def _shown_value(value: Any, cut: int | None) -> str:
    """A field's value as JSON, as JavaScript writes it (numbers by js_number), cut with a note."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return js_number(_as_double(value))
    if not isinstance(value, str) or cut is None or len(value) <= cut:
        return json.dumps(value, ensure_ascii=False)
    return f"{json.dumps(value[:cut], ensure_ascii=False)}… ({len(value) - cut} more characters not shown)"
