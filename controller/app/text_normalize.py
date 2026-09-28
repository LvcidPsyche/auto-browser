"""Arabic-aware text normalization for target/text matching.

Two authors of the same visible label rarely agree letter-for-letter in
Arabic: an LLM-authored target and the page's own markup commonly differ only
in a handful of visually-identical or dialectal letter variants (the
employee wrote "مدينة", the site shows "مدينه" -- a final taa marbuta vs haa),
a trailing tatweel (ـ) used for justification, or diacritics (harakat) the
page renders but the employee never typed. None of that is a real difference
to a person reading the page; comparing raw strings treats it as one and a
click-by-text or type-by-placeholder match fails every time. Normalize both
sides before comparing anywhere a target is matched against visible text.
"""

from __future__ import annotations

import re
import unicodedata

# Combining marks (harakat/tanwin/etc.), Quranic annotation marks, and the
# tatweel elongation character -- none of them change a word's identity.
_ARABIC_DIACRITICS_RE = re.compile(
    "[ؐ-ًؚ-ٰٟۖ-ۜ۟-۪ۨ-ۭـࣔ-ࣣ࣡-ࣿ]"
)

# Letter variants that read identically to a person but are distinct code
# points: canonicalize each family to one representative.
_ARABIC_CANON_MAP = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ة": "ه",
        "ى": "ي",
        "ئ": "ي",
        "ؤ": "و",
    }
)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_arabic_text(value: str | None) -> str:
    """Canonical form for comparing Arabic (and mixed Arabic/Latin) UI text.

    Strips diacritics/tatweel, canonicalizes letter variants, collapses
    whitespace, and case-folds -- so "مدينة" and "مدينه", or the same label
    with different internal spacing, compare equal. Non-Arabic text still
    benefits from the whitespace-collapse + case-fold pass."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = _ARABIC_DIACRITICS_RE.sub("", text)
    text = text.translate(_ARABIC_CANON_MAP)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text.casefold()


def arabic_aware_equals(a: str | None, b: str | None) -> bool:
    """True when both strings normalize to the same, non-empty text."""
    left, right = normalize_arabic_text(a), normalize_arabic_text(b)
    return bool(left) and left == right


def arabic_aware_contains(haystack: str | None, needle: str | None) -> bool:
    """True when either normalized string contains the other (both non-empty).

    Bidirectional on purpose: a target may be a short label inside a longer
    option string ("مدينة نصر" inside "مدينه نصر - المنطقه الثامنه"), or the
    other way around (a truncated visible label inside a longer target)."""
    left, right = normalize_arabic_text(haystack), normalize_arabic_text(needle)
    return bool(left) and bool(right) and (right in left or left in right)
