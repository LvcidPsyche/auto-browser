from __future__ import annotations

from app.text_normalize import arabic_aware_contains, arabic_aware_equals, normalize_arabic_text


def test_taa_marbuta_vs_haa_normalize_equal():
    # The employee wrote "مدينة" (taa marbuta); the site spells it "مدينه" (haa).
    assert normalize_arabic_text("مدينة") == normalize_arabic_text("مدينه")
    assert arabic_aware_equals("مدينة نصر", "مدينه نصر")


def test_alef_variants_normalize_equal():
    assert normalize_arabic_text("أحمد") == normalize_arabic_text("احمد")
    assert normalize_arabic_text("إحمد") == normalize_arabic_text("احمد")
    assert normalize_arabic_text("آحمد") == normalize_arabic_text("احمد")


def test_diacritics_and_tatweel_are_stripped():
    assert normalize_arabic_text("مُــدَرِّس") == normalize_arabic_text("مدرس")


def test_whitespace_collapsed_and_case_folded():
    assert normalize_arabic_text("  Submit   Now  ") == "submit now"


def test_empty_and_none_are_never_a_match():
    assert normalize_arabic_text(None) == ""
    assert normalize_arabic_text("") == ""
    assert not arabic_aware_equals(None, "")
    assert not arabic_aware_equals("شيء", "")


def test_contains_is_bidirectional():
    option = "مدينه نصر - المنطقه الثامنه (مدينه نصر)"
    target = "مدينة نصر"
    assert arabic_aware_contains(option, target)
    assert arabic_aware_contains(target, option)


def test_contains_does_not_match_unrelated_text():
    assert not arabic_aware_contains("الاسكندريه", "مدينة نصر")
