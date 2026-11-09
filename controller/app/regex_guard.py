"""Refuses the classic catastrophically backtracking regular expressions before they reach a page.

browser.find_elements' regex mode hands the pattern to the page's RegExp. A pattern such as (a+)+$ can hang the page's
JavaScript thread on the page's own text; the gateway's 10 s guard stops waiting for the read, not the regex, which goes
on running in the renderer. So a group with an unbounded repeat after it (*, +, {n,}) is refused when its body holds an
unbounded repeat of its own, or when its alternatives overlap (one is a prefix of another, or they are equal: the
classic (a|aa)+).

A small scanner over the pattern's syntax (escapes, character classes, groups), iterative and bounded by the pattern's
length, not a regular expression over a regular expression. It reads the pattern as the page's RegExp does: JavaScript's
syntax, with the case-insensitive flag the gateway uses. A malformed pattern is passed through for the page's RegExp to
refuse. The check stops the classic shapes and is not a proof a pattern is safe: the 10 s guard stays.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _Group:
    """A group while the scan is inside it (the outermost one is the pattern itself, and is never repeated)."""

    opened_at: int
    # Each alternative's text so far, case-folded where the page's case-insensitive matching ignores case.
    alternatives: list[str] = field(default_factory=lambda: [""])
    # Whether its body holds an unbounded repeat, at any depth.
    holds_repeat: bool = False


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
            parent.alternatives[-1] += "(" + "|".join(inner.alternatives) + ")" + pattern[index + 1 : end]
            index = end
        else:
            atom_end = _atom_end(pattern, index)
            end, unbounded = _quantifier(pattern, atom_end)
            group.holds_repeat = group.holds_repeat or unbounded
            atom = pattern[index:atom_end]
            group.alternatives[-1] += (atom.lower() if char not in "\\[" else atom) + pattern[atom_end:end]
            index = end
    return None


def _refusal(pattern: str, group: _Group, closed_at: int) -> str | None:
    text = pattern[group.opened_at : closed_at + 1]
    if group.holds_repeat:
        why = "repeats without bound and its body holds an unbounded repeat of its own"
    elif _alternatives_overlap(group.alternatives):
        why = "repeats without bound and its alternatives overlap (one is a prefix of another, or they are equal)"
    else:
        return None
    return (
        f"refused: the group {text!r} at index {group.opened_at} {why}, so this regular expression can backtrack "
        "catastrophically on the page's text and hang the page. Rewrite the group, or use regex=false to match the "
        "text literally."
    )


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
