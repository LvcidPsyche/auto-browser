"""
pii_cards.py — card numbers in text: the shape, the issuer prefix, the Luhn check, and the card that
sits inside a run of digit groups. Stdlib only: pii_scrub lists the pattern and does the redacting.
"""

from __future__ import annotations

import re

from .pii_views import Span

# 13-19 digits, with or without a space or dash between them. The shape takes as many groups as fit, so it
# also takes a quantity before a card or an expiry after one; `card_spans` finds the card inside.
CARD_SHAPE = r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])"
_SHAPE = re.compile(CARD_SHAPE)
_SEPARATOR = re.compile(r"[ -]")

# A card is written in at most five groups (4-4-4-4-3). A match of more is a list of numbers and not a card
# with something beside it, and trying every run of groups in it would make a page of digits cost more.
_MAX_GROUPS = 7


def _luhn_check(number: str) -> bool:
    """Return True if number passes Luhn algorithm."""
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) < 13:
        return False
    total = 0
    reverse = digits[::-1]
    for i, d in enumerate(reverse):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Issuer prefixes: Visa, Mastercard (51-55 and 2221-2720), Amex, Diners,
# Discover, JCB and the older 2131/1800 ranges — the set the previous pattern
# spelled out inline.
_CARD_PREFIX = re.compile(
    r"4|5[1-5]|2(?:22[1-9]|2[3-9]\d|[3-6]\d{2}|7[01]\d|720)|3[47]|3(?:0[0-5]|[68])|6(?:011|5)|35|2131|1800"
)


def _is_card_number(digits: str) -> bool:
    return 13 <= len(digits) <= 19 and _CARD_PREFIX.match(digits) is not None and _luhn_check(digits)


def looks_like_card(candidate: str) -> bool:
    return _is_card_number(re.sub(r"\D", "", candidate))


def _card_inside(matched: str) -> Span | None:
    """The card in a match of the shape, as positions in `matched`: all of it, or failing that the longest
    run of its whole digit groups that is one (`4111 1111 1111 1111 12` holds the card without the `12`)."""
    groups = _SEPARATOR.split(matched)
    digits = "".join(groups)
    if _is_card_number(digits):
        return 0, len(matched)
    # A card inside has lost the first group or the last, so it needs 13 digits without one of them.
    if not 2 <= len(groups) <= _MAX_GROUPS or max(len(digits) - len(groups[0]), len(digits) - len(groups[-1])) < 13:
        return None
    places: list[Span] = []  # where each group sits in `matched`; a separator of one character follows it
    position = 0
    for group in groups:
        places.append((position, position + len(group)))
        position += len(group) + 1
    best: Span | None = None
    best_digits = 0
    for first in range(len(groups)):
        window = ""
        for last in range(first, len(groups)):
            window += groups[last]
            if len(window) > 19:
                break
            if len(window) > best_digits and (first, last) != (0, len(groups) - 1) and _is_card_number(window):
                best, best_digits = (places[first][0], places[last][1]), len(window)
    return best


def card_spans(text: str) -> list[Span]:
    """The spans of `text` that are card numbers."""
    spans: list[Span] = []
    position = 0
    while (match := _SHAPE.search(text, position)) is not None:
        inside = _card_inside(match.group())
        if inside is None:
            position = match.end()
        else:
            spans.append((match.start() + inside[0], match.start() + inside[1]))
            # Search on from the card, not the match: what the match held beyond it may begin another.
            position = match.start() + inside[1]
    return spans
