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
    # Overlapping alternatives anywhere inside the repeated group: wrapping them adds nothing, and `((a|aa)b)+` is
    # the same trade as `(a+b)+`, refused for what it holds.
    ("((a|aa))+", "those of a group inside it"),
    ("(?:(a|aa))+", "those of a group inside it"),
    ("((?:a|aa))+", "those of a group inside it"),
    ("((a|a))+", "those of a group inside it"),
    ("((a|aa)b)+", "those of a group inside it"),
    ("(x(y(a|aa)))+", "those of a group inside it"),
    # Case folds as the page's RegExp folds it (ECMAScript's Canonicalize, no u flag): e-acute and E-acute are one.
    ("(é|É)+", "overlap"),
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
    # A wrapped group whose alternatives do not overlap, and overlapping ones no unbounded repeat encloses.
    "((foo|bar))+",
    "(a|aa)",
    "((a|aa))",
    "((a|aa)){2,3}",
    # ECMAScript's Canonicalize keeps these apart without the u flag: k and the Kelvin sign, s and the long s, and a
    # character whose upper case is not one character (the sharp s).
    "(k|K)+",
    "(s|ſ)+",
    "(ß|ss)+",
    # Past U+FFFF the page's RegExp, with no u flag, reads two surrogates, which have no case: Deseret's small and
    # capital long I stay apart.
    "(\U00010428|\U00010400)+",
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
    # A bound is no cure, and the message handed one out as if it were ((a{1,50})+$ passes the check and is as slow
    # as what was refused). It says which patterns a bound gets past the check, and that the others stay slow.
    assert "cannot match the same text in two ways" in message and "{1,50} in place of +" in message
    assert "is as slow with those bounds, which this check does not count" in message
    assert "Bound the group" not in message
    assert "regex=false" in message


# The patterns a bound is for: everyday ones the check refuses too (a separator between two repeats), which were
# never slow. Bounded as the refusal says, they pass.
BOUNDED_AS_THE_REFUSAL_SAYS = (
    (r"\d+(\.\d+)*", r"\d+(\.\d{1,50})*"),  # each repeat inside the group
    (r"\d+(\.\d+)*", r"\d+(\.\d+){0,50}"),  # the group's own
    (r"\w+(\s+\w+)*", r"\w+(\s{1,50}\w{1,50})*"),
    (r"(\w+,\s*)+\w+", r"(\w{1,50},\s{0,50})+\w+"),
    ("(Mr|Mrs)+", "(Mr|Mrs){1,50}"),
    ("((a|aa))+", "((a|aa)){1,50}"),
)


@pytest.mark.parametrize(("refused", "bounded"), BOUNDED_AS_THE_REFUSAL_SAYS)
def test_a_pattern_bounded_as_the_refusal_says_passes(refused: str, bounded: str) -> None:
    assert catastrophic_repeat(refused) is not None
    assert catastrophic_repeat(bounded) is None
    FindElementsInput(session_id=SESSION, query=bounded, regex=True)


def test_bounding_one_of_two_repeats_inside_the_group_is_not_enough() -> None:
    # Why the refusal says "each": the group still holds an unbounded repeat.
    assert catastrophic_repeat(r"\w+(\s{1,50}\w+)*") is not None


def test_the_refusal_of_overlapping_alternatives_names_the_groups_repeat_only() -> None:
    message = catastrophic_repeat("(Mr|Mrs)+")

    assert "with an upper bound on its repeat ({1,50} in place of +" in message
    assert "each repeat inside it" not in message


def test_the_refusal_of_a_repeat_inside_the_group_names_both_ways_to_bound_it() -> None:
    message = catastrophic_repeat(r"\d+(\.\d+)*")

    assert "with an upper bound on its repeat or on each repeat inside it ({1,50} in place of +" in message
    assert r"a separator between its repeats, as in \d+(\.\d+)*" in message


# What the check does not count, and the refusal now says so: a repeat with an upper bound, on either side. Each of
# these is as slow as the pattern it bounds ((a{1,50})+$ took Python's re 11 s on 26 letters and a miss).
BOUNDED_AND_AS_SLOW = ("(a{1,50})+$", "(a+){1,50}$", "(a|aa){1,50}$")


@pytest.mark.parametrize("pattern", BOUNDED_AND_AS_SLOW)
def test_a_bound_gets_a_slow_pattern_past_the_check_too(pattern: str) -> None:
    assert catastrophic_repeat(pattern) is None


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
