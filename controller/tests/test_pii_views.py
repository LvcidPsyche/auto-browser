"""The decoded views behind the PII scrubber: what they show, and how a stretch maps back."""

from __future__ import annotations

import json
import random

import pytest

from app import pii_views
from app.pii_views import (
    decode_form,
    decode_json_escapes,
    decode_json_token_escapes,
    inside_any,
    merge_found,
    merge_spans,
    replace_spans,
)


def test_form_view_decodes_percent_runs_and_plus_and_keeps_the_text_otherwise() -> None:
    view = decode_form("a=caf%C3%A9+au+lait&b=%40")

    assert view.text == "a=café au lait\nb=@"
    assert view.changed


def test_form_view_of_plain_text_is_not_changed_even_though_ampersands_become_newlines() -> None:
    view = decode_form("a=1&b=2")

    assert view.text == "a=1\nb=2"
    assert not view.changed


def test_plus_alone_counts_as_a_change() -> None:
    assert decode_form("q=a+b").changed


def test_to_raw_maps_a_match_across_a_percent_run_back_to_the_original() -> None:
    text = "email=ops%40example.com&z=1"
    view = decode_form(text)
    start = view.text.index("ops")
    end = view.text.index("\n")

    raw_start, raw_end = view.to_raw(start, end)

    assert text[raw_start:raw_end] == "ops%40example.com"


def test_a_stretch_that_touches_part_of_a_run_covers_all_of_it() -> None:
    text = "x=%C3%A9%2Bops"
    view = decode_form(text)
    plus = view.text.index("+")

    raw_start, raw_end = view.to_raw(plus, plus + 1)

    assert text[raw_start:raw_end] == "%C3%A9%2B"


def test_every_character_outside_a_run_maps_to_the_character_it_came_from() -> None:
    rng = random.Random(7)  # noqa: S311 (a seeded, repeatable corpus)
    pieces = ["abc", "%40", "%C3%A9", "%E9", "+", "&", "=", "x", "%2B%2F", "%zz", "%4", "é"]
    for _ in range(300):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 25)))
        view = decode_form(text)
        for index, char in enumerate(view.text):
            raw_start, raw_end = view.to_raw(index, index + 1)
            raw = text[raw_start:raw_end]
            assert raw_start < raw_end, (text, index)
            if len(raw) == 1:
                assert char == {"+": " ", "&": "\n"}.get(raw, raw), (text, index)
            else:
                assert raw.startswith("%"), (text, index)
        # The whole view maps to the whole text.
        assert view.to_raw(0, len(view.text)) == (0, len(text)), text


def _j(text: str) -> str:
    """JSON text with `¦` standing for the backslash that starts an escape (so that this file holds none)."""
    return text.replace("¦", chr(92))


def test_json_view_decodes_every_escape_except_the_ones_that_keep_the_text_valid_json() -> None:
    for code in range(0x10000):
        text = _j('"¦u%04x"' % code)
        structural = code < 0x20 or code in (0x22, 0x5C) or 0xD800 <= code <= 0xDFFF

        view = decode_json_escapes(text)

        if structural:
            assert (view.text, view.changed) == (text, False), hex(code)
        else:
            assert (view.text, view.changed) == ('"' + chr(code) + '"', True), hex(code)
        assert json.loads(view.text) == json.loads(text), hex(code)


def test_json_token_view_writes_out_only_the_escapes_of_token_characters() -> None:
    for code in range(0x10000):
        text = _j('"¦u%04x"' % code)
        char = chr(code)
        token_char = code < 0x80 and (char.isalnum() or char in "@._%+-")

        view = decode_json_token_escapes(text)

        if token_char:
            assert (view.text, view.changed) == ('"' + char + '"', True), hex(code)
        else:
            assert (view.text, view.changed) == (text, False), hex(code)


def test_json_token_view_reads_a_name_and_leaves_the_value_escapes_alone() -> None:
    text = _j('{"pass¦u0077ord":"p¦u0026ss¦u0020w0rd","a¦u002fb":1}')

    view = decode_json_token_escapes(text)

    assert view.text == _j('{"password":"p¦u0026ss¦u0020w0rd","a¦u002fb":1}')


def test_json_token_view_maps_back_the_same_way_the_full_view_does() -> None:
    text = _j('{"pass¦u0077ord":"x¦u0020y","e":"ops¦u0040example.com"}')
    token_view, full_view = decode_json_token_escapes(text), decode_json_escapes(text)
    start, end = token_view.text.index("password"), token_view.text.index("com") + 3

    raw_start, raw_end = token_view.to_raw(start, end)

    assert text[raw_start:raw_end] == text[text.index("pass") : text.index("com") + 3]
    assert token_view.snap(10, 12) == full_view.snap(10, 12)


def test_json_view_writes_out_the_short_escape_of_a_slash_and_no_other_short_escape() -> None:
    text = _j('{"a":"x¦/y","b":"¦n¦t¦r¦b¦f¦"¦¦"}')

    view = decode_json_escapes(text)

    assert view.text == _j('{"a":"x/y","b":"¦n¦t¦r¦b¦f¦"¦¦"}')
    assert view.changed
    assert json.loads(view.text) == json.loads(text)


def test_json_view_reads_escapes_pair_by_pair() -> None:
    # A doubled backslash before u0040 is an escaped backslash and then the text u0040, not an escaped @.
    text = _j('{"path":"C:¦¦u0040dir","a":"x¦u0040y"}')

    view = decode_json_escapes(text)

    assert view.text == _j('{"path":"C:¦¦u0040dir","a":"x@y"}')


def test_json_view_maps_a_decoded_stretch_back_to_the_escape_it_came_from() -> None:
    text = _j('{"e":"ops¦u0040example.com","n":"¦u00e9"}')
    view = decode_json_escapes(text)
    start, end = view.text.index("ops"), view.text.index("com") + 3

    raw_start, raw_end = view.to_raw(start, end)

    assert text[raw_start:raw_end] == _j("ops¦u0040example.com")


def test_json_view_snap_widens_a_stretch_that_cuts_an_escape_in_half() -> None:
    text = _j('{"a":"x¦nb@y","b":"¦u0020z"}')
    view = decode_json_escapes(text)
    newline = text.index("¦n".replace("¦", chr(92)))  # the escape `\\n`, kept escaped: 2 characters
    space = text.index(chr(92) + "u0020")  # a decoded escape: 6 characters

    assert view.snap(newline + 1, newline + 5) == (newline, newline + 5)  # starts on the `n`
    assert view.snap(newline - 1, newline + 1) == (newline - 1, newline + 2)  # ends after the backslash
    assert view.snap(space + 3, space + 9) == (space, space + 9)  # starts inside the u-escape
    assert view.snap(space - 2, space + 6) == (space - 2, space + 6)  # cuts nothing: unchanged


def test_empty_text_has_an_empty_unchanged_view() -> None:
    view = decode_form("")

    assert (view.text, view.changed) == ("", False)
    assert view.to_raw(0, 0) == (0, 0)


def test_merge_spans_joins_overlapping_and_touching_spans_only() -> None:
    assert merge_spans([(5, 8), (0, 2), (1, 3), (3, 4), (10, 12)]) == [(0, 4), (5, 8), (10, 12)]
    assert merge_spans([]) == []


def test_merge_found_keeps_one_hit_per_pattern_in_each_merged_span() -> None:
    def hit(pattern: str, tag: str) -> dict[str, object]:
        return {"pattern": pattern, "tag": tag}

    found = [
        (5, 9, hit("email", "late")),
        (0, 4, hit("bearer_token", "first")),
        (0, 6, hit("bearer_token", "longer")),
        (2, 3, hit("jwt_token", "inside")),
        (20, 22, hit("email", "apart")),
    ]

    spans, hits = merge_found(found)

    assert spans == [(0, 9), (20, 22)]
    assert [item["tag"] for item in hits] == ["first", "inside", "late", "apart"]
    assert merge_found([]) == ([], [])


def test_replace_spans_replaces_each_span_once() -> None:
    assert replace_spans("0123456789", [(1, 3), (5, 9)], "#") == "0#34#9"
    assert replace_spans("abc", [], "#") == "abc"
    assert replace_spans("abc", [(0, 3)], "#") == "#"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [(0, 5, True), (2, 5, True), (0, 6, False), (6, 8, False), (10, 12, True), (9, 12, False), (4, 4, True)],
)
def test_inside_any_needs_one_span_to_hold_the_whole_stretch(start: int, end: int, expected: bool) -> None:
    assert inside_any([(0, 5), (10, 12)], start, end) is expected


def test_module_is_stdlib_only() -> None:
    # The scrubber imports this module, so it must not import the scrubber back.
    assert "pii_scrub" not in vars(pii_views)
