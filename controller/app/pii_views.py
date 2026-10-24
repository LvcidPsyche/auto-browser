"""
pii_views.py — decoded views of encoded text, with the way back to the text as it arrived.

The PII patterns match plain characters, so `ops%40example.com` (a form value) and
`ops\\u0040example.com` (a JSON string) do not look like an email. The scrubber matches the
decoded view instead and then redacts the matching stretch of the original text, so everything
outside a match keeps the bytes it arrived with. Stdlib only: the patterns live in pii_scrub.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

Span = tuple[int, int]
Found = tuple[int, int, dict[str, Any]]  # a stretch of the text to redact, and the hit that says why

_PERCENT_RUN = re.compile(r"(?:%[0-9A-Fa-f]{2})+")

# Escapes are read pair by pair, so the second backslash of an escaped backslash does not start one:
# the text after it is just text.
_JSON_ESCAPE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|[\s\S])")


@dataclass(frozen=True)
class DecodedText:
    """`text` is the decoded view; each chunk is a stretch of the original that decoding rewrote whole.

    Outside the chunks the two texts agree character for character, so a position in the view maps back
    to the original by the offset the chunks before it added. A match that touches part of a chunk covers
    all of it in the original: half of an escape or of a `%XX` run cannot be redacted on its own.
    """

    text: str
    changed: bool
    _view_starts: tuple[int, ...] = ()
    _view_ends: tuple[int, ...] = ()
    _raw_starts: tuple[int, ...] = ()
    _raw_ends: tuple[int, ...] = ()

    def to_raw(self, start: int, end: int) -> Span:
        """The stretch of the original that the view's `text[start:end]` was decoded from."""
        return self._raw_position(start, end=False), self._raw_position(end, end=True)

    def _raw_position(self, position: int, *, end: bool) -> int:
        last = position - 1 if end else position  # the character the position stands on
        k = bisect_right(self._view_starts, last) - 1
        if k < 0:
            return position
        if last < self._view_ends[k]:
            return self._raw_ends[k] if end else self._raw_starts[k]
        return position + self._raw_ends[k] - self._view_ends[k]

    def snap(self, start: int, end: int) -> Span:
        """Widen a stretch of the original so that it cuts no chunk in half."""
        i = bisect_right(self._raw_starts, start) - 1
        if i >= 0 and start < self._raw_ends[i]:
            start = self._raw_starts[i]
        j = bisect_left(self._raw_starts, end) - 1
        if j >= 0 and end < self._raw_ends[j]:
            end = self._raw_ends[j]
        return start, end


def _decode(
    text: str,
    pattern: re.Pattern[str],
    decode: Callable[[re.Match[str]], str],
    plain: Callable[[str], str] = str,
    plain_changes: bool = False,
) -> DecodedText:
    """Rewrite each match of `pattern` with `decode(match)` and the text between matches with `plain`.

    `plain` must keep the length of what it is given. The view counts as `changed` when a match decodes
    to something else, or when `plain_changes` says that `plain` rewrote something that matters.
    """
    pieces: list[str] = []
    view_starts: list[int] = []
    view_ends: list[int] = []
    raw_starts: list[int] = []
    raw_ends: list[int] = []
    position = length = 0
    changed = False
    for match in pattern.finditer(text):
        before = plain(text[position : match.start()])
        decoded = decode(match)
        length += len(before)
        view_starts.append(length)
        length += len(decoded)
        view_ends.append(length)
        raw_starts.append(match.start())
        raw_ends.append(match.end())
        changed = changed or decoded != match.group()
        pieces += (before, decoded)
        position = match.end()
    pieces.append(plain(text[position:]))
    return DecodedText(
        "".join(pieces),
        changed or plain_changes,
        tuple(view_starts),
        tuple(view_ends),
        tuple(raw_starts),
        tuple(raw_ends),
    )


def _decode_percent_run(match: re.Match[str]) -> str:
    # What urllib.parse.unquote(..., errors="replace") returns for a run of `%XX`, at a tenth of the cost.
    return bytes.fromhex(match.group().replace("%", "")).decode("utf-8", errors="replace")


def decode_form(text: str) -> DecodedText:
    """`text` read as application/x-www-form-urlencoded: `+` is a space, a `%XX` run is UTF-8 bytes.

    A literal `&` becomes a newline in the view, so that a match cannot run on from one parameter into
    the next (`api_key_param` takes `&` as part of a value). It is one character for one, so positions
    outside the `%XX` runs still agree.
    """
    return _decode(
        text,
        _PERCENT_RUN,
        _decode_percent_run,
        plain=lambda literal: literal.replace("+", " ").replace("&", "\n"),
        plain_changes="+" in text,
    )


def _decode_json_escape(match: re.Match[str]) -> str:
    code = match.group(1)
    if code is None:
        # `\/` is the one short escape that is not needed: a slash is valid in a string as it is, and PHP's
        # json_encode escapes every one. The rest (n, a quote, a backslash...) stay as they are.
        return "/" if match.group() == "\\/" else match.group()
    char = chr(int(code, 16))
    # Written out, a quote or backslash would end the string or start another escape, a control character
    # is not allowed inside one, and half of a surrogate pair cannot be encoded. These stay escaped; every
    # other character is written out, so valid JSON in is valid JSON out.
    if char in ('"', "\\") or char < " " or 0xD800 <= ord(char) <= 0xDFFF:
        return match.group()
    return char


def decode_json_escapes(text: str) -> DecodedText:
    """`text` with each u-escape and each `\\/` written out as its character, except the ones that must stay escaped."""
    return _decode(text, _JSON_ESCAPE, _decode_json_escape)


# The characters a match runs through when it is a name or a token: letters, digits and the punctuation of
# an email address or a base64 string. Written out, anything else (a space, `&`, `'`) can end a match early.
_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@._%+-")


def _decode_token_escape(match: re.Match[str]) -> str:
    code = match.group(1)
    if code is None:
        return match.group()
    char = chr(int(code, 16))
    return char if char in _TOKEN_CHARS else match.group()


def decode_json_token_escapes(text: str) -> DecodedText:
    """`text` with only the u-escapes of token characters written out; every other escape stays as it is.

    This is the view for what the fully written-out one cuts short: a `pass\\u0077ord` field name reads as
    `password`, while a value such as `p\\u0026ssw0rd!` stays one run of characters for `password_field`.
    """
    return _decode(text, _JSON_ESCAPE, _decode_token_escape)


def inside_any(spans: list[Span], start: int, end: int) -> bool:
    """Whether one of the merged (sorted, non-overlapping) `spans` holds all of start..end."""
    i = bisect_right(spans, (start, float("inf"))) - 1
    return i >= 0 and spans[i][1] >= end


def merge_spans(spans: Iterable[Span]) -> list[Span]:
    """Sort spans and join the ones that overlap or touch."""
    merged: list[Span] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def merge_found(found: Iterable[Found]) -> tuple[list[Span], list[dict[str, Any]]]:
    """The merged (sorted, non-overlapping) spans of `found`, and one hit per pattern in each of them.

    Two views of one text match the same redaction to different lengths, and each match is a hit, so the
    hits of a merged span are cut to the first one of each pattern. A span that two patterns made up
    keeps a hit for each, since the audit report lists the patterns that fired.
    """
    spans: list[Span] = []
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for start, end, hit in sorted(found, key=lambda item: item[:2]):
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(end, spans[-1][1]))
        else:
            spans.append((start, end))
            seen = set()
        if hit["pattern"] not in seen:
            seen.add(hit["pattern"])
            hits.append(hit)
    return spans, hits


def replace_spans(text: str, spans: Iterable[Span], replacement: str) -> str:
    """`text` with each of the (sorted, non-overlapping) spans replaced by `replacement`."""
    pieces: list[str] = []
    position = 0
    for start, end in spans:
        pieces += (text[position:start], replacement)
        position = end
    pieces.append(text[position:])
    return "".join(pieces)
