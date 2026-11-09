"""browser.find_elements refuses the classic catastrophically backtracking regexes before they reach the page.

A pattern such as (a+)+$ can hang the page's JavaScript thread: the controller's 10 s guard stops waiting for the read,
and the page's RegExp keeps running. The check is a scanner over the pattern's own syntax (escapes, classes, groups).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.regex_guard import catastrophic_repeat
from app.tool_inputs import FindElementsInput

SESSION = "s1"

REFUSED = (
    ("(a+)+$", "unbounded repeat of its own"),
    ("(.*)*", "unbounded repeat of its own"),
    ("(a|aa)+", "overlap"),
    ("(?:a+)+", "unbounded repeat of its own"),
    ("((a+))+", "unbounded repeat of its own"),
    ("(a*){2,}", "unbounded repeat of its own"),
    ("(a{2,})*", "unbounded repeat of its own"),
    ("(a+?)+", "unbounded repeat of its own"),
    ("(?<word>a+)+", "unbounded repeat of its own"),
    # Matching is case-insensitive in the page, so these are equal alternatives there.
    ("(a|A)+", "overlap"),
    ("(a|a)+", "overlap"),
    ("(|a)+", "overlap"),
)
ALLOWED = (
    "a+b",
    "(foo|bar)",
    "(foo|bar)+",
    "x{2,3}",
    "(ab)+",
    # A class, not a group.
    "[(a+)+]",
    r"\(a+\)+",
    "[a-z]+",
    r"\d+-\d+",
    # A bounded repeat, or none: a group whose body repeats is only dangerous repeated without bound.
    "(a+)?",
    "(a+){2,3}",
    "(a+)",
    # Escapes a case-folding comparison must not merge.
    r"(\d|\D)+",
    # Malformed patterns are the page's RegExp to refuse; the scan does not crash on them.
    "(",
    ")",
    "((a)",
    "[",
    "a{",
    "\\",
    "(?",
    "(?<",
)


@pytest.mark.parametrize(("pattern", "why"), REFUSED)
def test_a_catastrophic_pattern_is_refused_and_the_message_says_what_and_why(pattern: str, why: str) -> None:
    with pytest.raises(ValidationError) as raised:
        FindElementsInput(session_id=SESSION, query=pattern, regex=True)

    message = str(raised.value)
    assert why in message and "backtrack catastrophically" in message


@pytest.mark.parametrize("pattern", ALLOWED)
def test_a_safe_or_malformed_pattern_passes(pattern: str) -> None:
    assert catastrophic_repeat(pattern) is None
    FindElementsInput(session_id=SESSION, query=pattern, regex=True)


@pytest.mark.parametrize(("pattern", "why"), REFUSED)
def test_the_same_text_with_regex_false_is_only_text(pattern: str, why: str) -> None:
    payload = FindElementsInput(session_id=SESSION, query=pattern, regex=False)

    assert payload.query == pattern


def test_a_selector_lookup_is_not_scanned() -> None:
    FindElementsInput(session_id=SESSION, selector="(a+)+")


def test_the_refusal_names_the_group_that_was_refused() -> None:
    assert "(b+)" in catastrophic_repeat("ok(b+)*")


def test_a_group_in_a_long_alternation_is_scanned_all_the_same() -> None:
    assert catastrophic_repeat("safe|(a+)+") is not None


def test_a_bounded_inner_repeat_is_not_refused() -> None:
    assert catastrophic_repeat("(a{2,3})+") is None
