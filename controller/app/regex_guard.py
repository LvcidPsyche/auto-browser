"""Refuses the classic catastrophically backtracking regular expressions before they reach a page.

browser.find_elements' regex mode hands the pattern to the page's RegExp. A pattern such as (a+)+$ can hang the page's
JavaScript thread on the page's own text; the gateway's 10 s guard stops waiting for the read, not the regex, which goes
on running in the renderer. So a group with an unbounded repeat after it (*, +, {n,}) is refused when its body holds an
unbounded repeat of its own, or when its alternatives, or those of any group inside it, overlap (one is a prefix of
another, or they are equal: the classic (a|aa)+, and ((a|aa))+).

A small scanner over the pattern's syntax (escapes, character classes, groups), iterative and bounded by the pattern's
length, not a regular expression over a regular expression. It reads the pattern as the page's RegExp does: JavaScript's
syntax, with the case-insensitive flag the gateway uses and no u flag, so a literal character folds as ECMAScript's
Canonicalize folds it. A malformed pattern is passed through for the page's RegExp to refuse. Escapes, classes and a
nested group's parentheses are compared as they are written, so \\x61 against a, [a] against a, or (a) against a is not
seen, and a group's inline modifiers ((?-i:...)) are not read: its letters fold as the whole pattern's do. That is the
check's limit: it stops the classic shapes and is not a proof a pattern is safe, and the 10 s guard stays.

A repeat with an upper bound is not counted, around the group or inside it: (a{1,50})+$, (a+){1,50}$ and (a|aa){1,50}$
pass, and each is as slow as the pattern it bounds ((a{1,50})+$ took Python's re 11 s on 26 letters and a miss). So the
refusal offers a bound only for a group that cannot match the same text in two ways (a separator between its repeats,
as in \\d+(\\.\\d+)*): such a pattern was never slow, is refused for its shape alone, and a bound gets it past.

Selectors are not covered. A Playwright selector carries regular expressions of its own, and one given to an action or
to browser.wait_for_selector reaches the page as written: text=/(a+)+$/, :text-matches("(a+)+$") and
role=button[name=/(a+)+$/] each left a page of thirty letters answering nothing (real Chromium, Playwright 1.64).
Nothing here scans them, because the pattern is not always in the selector's text as written. Playwright 1.64 reads
one from a /.../ body (text, internal:text, internal:has-text, internal:has-not-text,
internal:label), from an attribute value (role, internal:role, internal:attr, internal:testid), from the string given
to the CSS function :text-matches(), also inside :has() or :is(), where its CSS tokenizer first decodes
escapes in the function's name and in the string (:text-m\\61 tches("\\28 a+\\29 +$") hung the page the same), from a
selector nested as a JSON string (internal:has, internal:has-not, internal:and, internal:or, internal:chain, left-of,
right-of, above, below, near, where "text=/\\u0028a+)+$/" hung it too), and from the JSON template of aria-template=.
A scan that misses none of them reads a selector as Playwright's own parsers do, at every version of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ECMAScript's Canonicalize keeps a character at or past U+0080 whose upper case falls below it.
_CANONICALIZE_KEEPS_FROM = 0x80
# Past this, a pattern with no u flag is read as two surrogates, which have no case.
_LAST_UTF16_UNIT = 0xFFFF


@dataclass
class _Group:
    """A group while the scan is inside it (the outermost one is the pattern itself, and is never repeated)."""

    opened_at: int
    # Each alternative's text so far, a literal character case-folded as the page's matching folds it (_canonicalized).
    alternatives: list[str] = field(default_factory=lambda: [""])
    # Whether its body holds an unbounded repeat, at any depth.
    holds_repeat: bool = False
    # Whether a group inside it has overlapping alternatives, at any depth.
    holds_overlap: bool = False


def catastrophic_repeat(pattern: str) -> str | None:
    """Why ``pattern`` is a catastrophic shape, or None when the scan finds none."""
    stack = [_Group(opened_at=-1)]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        group = stack[-1]
        if char == "(":
            stack.append(_Group(opened_at=index))
            index = _body_start(pattern, index)
        elif char == "|":
            group.alternatives.append("")
            index += 1
        elif char == ")" and len(stack) > 1:
            inner = stack.pop()
            parent = stack[-1]
            end, unbounded = _quantifier(pattern, index + 1)
            if unbounded and (reason := _refusal(pattern, inner, index)):
                return reason
            parent.holds_repeat = parent.holds_repeat or inner.holds_repeat or unbounded
            parent.holds_overlap = (
                parent.holds_overlap or inner.holds_overlap or _alternatives_overlap(inner.alternatives)
            )
            parent.alternatives[-1] += "(" + "|".join(inner.alternatives) + ")" + pattern[index + 1 : end]
            index = end
        else:
            atom_end = _atom_end(pattern, index)
            end, unbounded = _quantifier(pattern, atom_end)
            group.holds_repeat = group.holds_repeat or unbounded
            atom = pattern[index:atom_end]
            group.alternatives[-1] += (_canonicalized(atom) if char not in "\\[" else atom) + pattern[atom_end:end]
            index = end
    return None


def _refusal(pattern: str, group: _Group, closed_at: int) -> str | None:
    text = pattern[group.opened_at : closed_at + 1]
    # A bound is not offered as a cure: the scan stops counting a repeat with an upper bound, and (a{1,50})+$ is as
    # slow as (a+)+$. It helps only the everyday patterns this refuses too, which were never slow: it gets them past.
    if group.holds_repeat:
        why = "repeats without bound and its body holds an unbounded repeat of its own"
        unambiguous = (
            "A group that cannot match the same text in two ways (a separator between its repeats, as in "
            r"\d+(\.\d+)*)"
        )
        bound = "an upper bound on its repeat or on each repeat inside it"
    elif group.holds_overlap or _alternatives_overlap(group.alternatives):
        why = (
            "repeats without bound and its alternatives, or those of a group inside it, overlap (one is a prefix of "
            "another, or they are equal)"
        )
        unambiguous = "A group that cannot match the same text in two ways"
        bound = "an upper bound on its repeat"
    else:
        return None
    return (
        f"refused: the group {text!r} at index {group.opened_at} {why}, so this regular expression can backtrack "
        f"catastrophically on the page's text and hang the page. {unambiguous} passes this check with {bound} "
        "({1,50} in place of +, {0,50} in place of *); one that can is as slow with those bounds, which this check "
        "does not count. Or use regex=false to match the text literally."
    )


def _canonicalized(character: str) -> str:
    """A literal character as the page's case-insensitive RegExp compares it (ECMAScript's Canonicalize, no u flag)."""
    upper = character.upper()
    if ord(character) > _LAST_UTF16_UNIT or len(upper) != 1:
        return character
    if ord(character) >= _CANONICALIZE_KEEPS_FROM and ord(upper) < _CANONICALIZE_KEEPS_FROM:
        return character
    return upper


def _alternatives_overlap(alternatives: list[str]) -> bool:
    ordered = sorted(alternatives)
    # After sorting, a string that has another as a prefix comes right after it, or after one that also does.
    return any(later.startswith(earlier) for earlier, later in zip(ordered, ordered[1:]))


def _atom_end(pattern: str, index: int) -> int:
    """Where the one atom at ``index`` (an escape, a character class, or a single character) ends."""
    if pattern[index] == "\\":
        return index + 2
    if pattern[index] != "[":
        return index + 1
    # A class ends at the first unescaped ]: JavaScript has no [] or [^]] exception as other engines do.
    position = index + 1
    while position < len(pattern):
        if pattern[position] == "\\":
            position += 2
        elif pattern[position] == "]":
            return position + 1
        else:
            position += 1
    return len(pattern)


def _body_start(pattern: str, opened_at: int) -> int:
    """Where the body of the group opened at ``opened_at`` begins, past its (?: prefix if it has one."""
    index = opened_at + 1
    for prefix in ("?:", "?=", "?!", "?<=", "?<!"):
        if pattern.startswith(prefix, index):
            return index + len(prefix)
    if pattern.startswith("?<", index):
        close = pattern.find(">", index)
        return close + 1 if close != -1 else len(pattern)
    return index


def _quantifier(pattern: str, index: int) -> tuple[int, bool]:
    """Where the quantifier at ``index`` ends (``index`` itself when there is none), and whether it is unbounded."""
    if index >= len(pattern):
        return index, False
    char = pattern[index]
    if char in "*+":
        end, unbounded = index + 1, True
    elif char == "?":
        end, unbounded = index + 1, False
    elif char == "{":
        close = pattern.find("}", index)
        low, comma, high = pattern[index + 1 : close].partition(",") if close != -1 else ("", "", "")
        if not _is_number(low) or (high and not _is_number(high)):
            return index, False  # JavaScript reads this as a literal brace ({,3}, {ab}), not a quantifier.
        end, unbounded = close + 1, bool(comma) and not high
    else:
        return index, False
    # A lazy repeat (+?) only tries its options in another order, and can still backtrack over all of them.
    return end + 1 if pattern[end : end + 1] == "?" else end, unbounded


def _is_number(text: str) -> bool:
    return text.isascii() and text.isdigit()
