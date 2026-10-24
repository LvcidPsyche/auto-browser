"""The PII scrubber: linear on hostile input, and it finds what it says it finds.

* The email and JWT patterns were quadratic. Text with a word boundary at
  every character ("a.a.a.…") made the email pattern rescan to the end from
  each one: 40 KB took 3.4 s, and console text, which a page controls, was
  scrubbed on the event loop — one console.log froze the whole controller.
* Card numbers written in groups ("4111 1111 1111 1111") were never matched,
  although the pattern's comment promised separators, and provider API tokens
  and Basic credentials were not covered at all.
* Screenshot redaction scrubbed each OCR word on its own, so anything with a
  space in it — "(555) 123-4567", a grouped card number — never matched.
"""

from __future__ import annotations

import io
import time

import pytest
from PIL import Image

from app.pii_scrub import ALL_PATTERN_NAMES, PiiScrubber, find_pii_spans, scrub_screenshot, scrub_text

HOSTILE_UNITS = (
    "a.",
    "a@",
    "a.a@",
    "a-",
    "eyJ",
    "eyJa.",
    "1 ",
    "1-",
    "+1 ",
    "4",
    "4111 ",
    "x=",
    "key=",
    "Bearer ",
    "@a.",
    "-----BEGIN A B C D ",
    "-----BEGIN RSA PRIVATE KEY-----",
    "a@a.",
)


@pytest.mark.parametrize("unit", HOSTILE_UNITS)
def test_every_pattern_is_linear_on_hostile_input(unit: str) -> None:
    text = unit * (200_000 // len(unit))
    started = time.perf_counter()
    scrub_text(text)  # every pattern, the noisy ones included
    elapsed = time.perf_counter() - started
    # Linear runs take well under a second; the quadratic email pattern needed ~85 s here.
    assert elapsed < 5, f"{unit!r} x {len(text)} chars took {elapsed:.1f}s"


def _scrubbed(text: str) -> str:
    return PiiScrubber(enabled_patterns=set(ALL_PATTERN_NAMES)).text(text).text


# Fake tokens are assembled from pieces so the source never holds a complete
# one: GitHub push protection refuses any push containing a token-shaped
# string, fake or not.
@pytest.mark.parametrize(
    "secret",
    [
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "4111111111111111",
        "3782 822463 10005",
        "5555 5555 5555 4444",
        "2223 0031 2200 3222",
        "sk-proj-" + "Ab3_" * 15,
        "sk-ant-api03-" + "Zx9-" * 15,
        "sk-" + "a1B2" * 12,
        "ghp_" + "A1b2" * 9,
        "github_pat_" + "11ABCDEFG0" * 4,
        "xoxb-" + "1234567890-0987654321-abcdefghijklmnop",
        "AIza" + "SyA-abcdefghijklmnopqrstuvwxyz12345",
        "hf_" + "aBcD" * 9,
        "glpat-" + "x1Y2z3" * 4,
    ],
)
def test_secrets_are_redacted(secret: str) -> None:
    assert secret not in _scrubbed(f"value: {secret} end")


@pytest.mark.parametrize(
    "line,secret",
    [
        ("Authorization: Basic " + "dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("Cookie: sessionid=abc123def456ghi789", "abc123def456ghi789"),
        ("set-cookie: auth=s3cr3tvalue123; Path=/", "s3cr3tvalue123"),
        ("https://x.example/cb?code=4/0AY0e-g7abcdefghij&state=xyz", "4/0AY0e-g7abcdefghij"),
    ],
)
def test_credentials_in_headers_and_callbacks_are_redacted(line: str, secret: str) -> None:
    assert secret not in _scrubbed(line)


@pytest.mark.parametrize(
    "text",
    [
        "4111 1111 1111 1112",  # fails Luhn
        "9999 9999 9999 9995",  # no card issuer starts with 99
        "Basic Information about the plan",
        "Cookie: this site uses cookies",
        "country code=US",
        "the skeleton-loading-spinner-wrapper class",
        "order 2026-10-01 shipped",
    ],
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    assert _scrubbed(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The pattern takes every digit group that fits, so the expiry made the whole match fail Luhn.
        ("4111 1111 1111 1111 12/27", "[REDACTED] 12/27"),
        ("4111 1111 1111 1111 12 27", "[REDACTED] 12 27"),
        ("4111-1111-1111-1111-12", "[REDACTED]-12"),
        ("3782 822463 10005 12 27", "[REDACTED] 12 27"),
        ("qty 3 4111 1111 1111 1111", "qty 3 [REDACTED]"),
        ("qty 12 4111 1111 1111 1111 9", "qty 12 [REDACTED] 9"),
        ("5555 5555 5555 4444 and 4111 1111 1111 1111 12", "[REDACTED] and [REDACTED] 12"),
        # What the match held beyond the card may begin the next one.
        ("3782 822463 10005 4111 1111 1111 1111", "[REDACTED] [REDACTED]"),
        ("5555 5555 5555 4444 4111 1111 1111 1111", "[REDACTED] [REDACTED]"),
    ],
)
def test_a_card_next_to_another_group_of_digits_is_redacted_without_the_group(text: str, expected: str) -> None:
    result = scrub_text(text, enabled_patterns={"credit_card"})

    assert result.text == expected
    assert [hit["pattern"] for hit in result.hits] == ["credit_card"] * expected.count("[REDACTED]")


@pytest.mark.parametrize(
    "text",
    [
        "9999 9999 9999 9999 99",  # no issuer starts with 9
        "0000 0000 0000 0000 12",
        "1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9",
        "4111 1111 1111",  # twelve digits
        "2026 10 24 11 30 59 0123 4567",
    ],
)
def test_digit_groups_that_hold_no_card_are_left_alone(text: str) -> None:
    assert scrub_text(text, enabled_patterns={"credit_card"}).text == text


def test_card_hit_describes_the_card_and_not_the_digits_beside_it() -> None:
    result = scrub_text("pay 4111 1111 1111 1111 12/27", enabled_patterns={"credit_card"})

    assert [(hit["offset"], hit["length"]) for hit in result.hits] == [(4, len("4111 1111 1111 1111"))]
    assert find_pii_spans("pay 4111 1111 1111 1111 12/27", {"credit_card"}) == [(4, 23, "credit_card")]


def test_screenshot_redaction_leaves_the_words_beside_a_card_alone() -> None:
    blank = io.BytesIO()
    Image.new("RGB", (400, 100), (255, 255, 255)).save(blank, format="PNG")
    words = [_word(text, 10 + 50 * i, 30, (1, 1, 1)) for i, text in enumerate(("4111", "1111", "1111", "1111", "12"))]

    redacted, hits = scrub_screenshot(blank.getvalue(), words)
    image = Image.open(io.BytesIO(redacted)).convert("RGB")

    assert all(_is_black(image, block) for block in words[:4])
    assert not _is_black(image, words[4])
    assert {hit["pattern"] for hit in hits} == {"credit_card"}


def _word(text: str, x: int, y: int, line: tuple[int, int, int]) -> dict:
    return {"text": text, "line": list(line), "bbox": {"x": x, "y": y, "width": 40, "height": 20}}


def _is_black(image: Image.Image, block: dict) -> bool:
    box = block["bbox"]
    return image.getpixel((box["x"] + box["width"] // 2, box["y"] + box["height"] // 2)) == (0, 0, 0)


def test_screenshot_redaction_matches_across_the_words_of_a_line() -> None:
    blank = io.BytesIO()
    Image.new("RGB", (400, 140), (255, 255, 255)).save(blank, format="PNG")
    phone = [_word("Call", 10, 10, (1, 1, 1)), _word("(555)", 60, 10, (1, 1, 1)), _word("123-4567", 110, 10, (1, 1, 1))]
    card = [_word(group, 10 + 50 * i, 50, (1, 1, 2)) for i, group in enumerate(("4111", "1111", "1111", "1111"))]
    split = [_word("(555)", 10, 90, (1, 1, 3)), _word("123-4567", 60, 110, (1, 1, 4))]  # different lines

    redacted, hits = scrub_screenshot(blank.getvalue(), phone + card + split)
    image = Image.open(io.BytesIO(redacted)).convert("RGB")

    assert not _is_black(image, phone[0])
    assert all(_is_black(image, block) for block in phone[1:])
    assert all(_is_black(image, block) for block in card)
    assert not any(_is_black(image, block) for block in split)
    assert {hit["pattern"] for hit in hits} >= {"phone_us", "credit_card"}


def test_real_ocr_output_carries_its_lines_into_redaction(tmp_path, monkeypatch) -> None:
    """Across the OCR -> scrubber boundary, not against a hand-written block shape."""
    from app import ocr as ocr_module

    image = tmp_path / "shot.png"
    Image.new("RGB", (300, 60), (255, 255, 255)).save(image)
    data = {
        "text": ["Call", "(555)", "123-4567"],
        "conf": ["96", "95", "94"],
        "left": [10, 60, 130],
        "top": [10, 10, 10],
        "width": [40, 50, 80],
        "height": [20, 20, 20],
        "block_num": [1, 1, 1],
        "par_num": [1, 1, 1],
        "line_num": [1, 1, 1],
    }
    monkeypatch.setattr(ocr_module.pytesseract, "image_to_data", lambda *args, **kwargs: data)
    payload = ocr_module.OCRExtractor(enabled=True, language="eng", max_blocks=20, text_limit=2000)._extract_sync(image)

    assert [block["line"] for block in payload["redaction_blocks"]] == [[1, 1, 1]] * 3
    assert all("line" not in block for block in payload["blocks"]), "the model-facing blocks keep their shape"
    redacted, _ = scrub_screenshot(image.read_bytes(), payload["redaction_blocks"])
    out = Image.open(io.BytesIO(redacted)).convert("RGB")
    assert out.getpixel((85, 20)) == (0, 0, 0)
    assert out.getpixel((170, 20)) == (0, 0, 0)
    assert out.getpixel((30, 20)) == (255, 255, 255)


def test_console_text_is_capped_when_it_is_captured() -> None:
    from types import SimpleNamespace

    from app.browser.services.diagnostics import CONSOLE_TEXT_MAX_CHARS, console_entry

    entry = console_entry(SimpleNamespace(type="log", text="a." * 100_000, location={}))
    assert len(entry["text"]) < CONSOLE_TEXT_MAX_CHARS + 100
    assert entry["text"].endswith("more characters]")
    assert console_entry(SimpleNamespace(type="log", text="short", location={}))["text"] == "short"


def test_single_word_redaction_still_works_without_line_information() -> None:
    blank = io.BytesIO()
    Image.new("RGB", (200, 60), (255, 255, 255)).save(blank, format="PNG")
    email = {"text": "alice@example.com", "bbox": {"x": 10, "y": 10, "width": 120, "height": 20}}
    redacted, hits = scrub_screenshot(blank.getvalue(), [email])
    assert _is_black(Image.open(io.BytesIO(redacted)).convert("RGB"), email)
    assert hits and hits[0]["pattern"] == "email"
