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

from app.pii_scrub import ALL_PATTERN_NAMES, PiiScrubber, scrub_screenshot, scrub_text

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
