"""observe handed back the OCR words the screenshot redaction had just blacked out."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.pii_scrub import PiiScrubber
from tests.test_screenshot_scrub_unavailable import _service

WORDS = ["Contact", "ops@example.com", "card", "4111", "1111", "1111", "1111"]
EMAIL = "ops@example.com"
OCR_DATA = {
    "text": WORDS,
    "conf": ["95"] * len(WORDS),
    "left": [10 * i for i in range(len(WORDS))],
    "top": [5] * len(WORDS),
    "width": [9] * len(WORDS),
    "height": [9] * len(WORDS),
    "block_num": [1] * len(WORDS),
    "par_num": [1] * len(WORDS),
    "line_num": [1] * len(WORDS),
}


async def _observe(tmp_path: Path, *, scrub_enabled: bool = True) -> dict:
    service = _service(tmp_path)
    service.manager.settings.ocr_skip_when_text_available = False
    if not scrub_enabled:
        service.manager.pii_scrubber = PiiScrubber(enabled=False)
    with patch("app.ocr.pytesseract.image_to_data", return_value=OCR_DATA):
        return await service.observation_payload(service.manager.session, preset="normal")


async def test_observe_does_not_return_the_redaction_word_list(tmp_path: Path) -> None:
    out = await _observe(tmp_path)

    assert "redaction_blocks" not in out["ocr"]


async def test_observe_scrubs_the_ocr_text(tmp_path: Path) -> None:
    out = await _observe(tmp_path)

    assert EMAIL not in out["ocr"]["text_excerpt"]
    assert EMAIL not in [block["text"] for block in out["ocr"]["blocks"]]
    assert out["ocr"]["text_excerpt"].startswith("Contact")


async def test_observe_leaves_the_ocr_text_alone_when_scrubbing_is_off(tmp_path: Path) -> None:
    out = await _observe(tmp_path, scrub_enabled=False)

    assert EMAIL in out["ocr"]["text_excerpt"]
    assert "redaction_blocks" not in out["ocr"]


async def test_observe_redacts_a_card_number_split_across_blocks(tmp_path: Path) -> None:
    out = await _observe(tmp_path)

    texts = [block["text"] for block in out["ocr"]["blocks"]]
    assert "4111" not in texts
    assert "4111" not in " ".join(texts)
    assert "4111" not in out["ocr"]["text_excerpt"]


async def test_observe_redacts_a_phone_number_split_across_blocks(tmp_path: Path) -> None:
    words = ["Call", "(555)", "123-4567", "today"]
    data = {
        **OCR_DATA,
        "text": words,
        "conf": ["95"] * len(words),
        "left": [10 * i for i in range(len(words))],
        "top": [5] * len(words),
        "width": [9] * len(words),
        "height": [9] * len(words),
        "block_num": [1] * len(words),
        "par_num": [1] * len(words),
        "line_num": [1] * len(words),
    }
    service = _service(tmp_path)
    service.manager.settings.ocr_skip_when_text_available = False
    with patch("app.ocr.pytesseract.image_to_data", return_value=data):
        out = await service.observation_payload(service.manager.session, preset="normal")

    texts = [block["text"] for block in out["ocr"]["blocks"]]
    assert "(555)" not in texts
    assert "123-4567" not in texts


async def test_observe_keeps_an_ordinary_word_block_as_it_was(tmp_path: Path) -> None:
    out = await _observe(tmp_path)

    first = out["ocr"]["blocks"][0]
    assert first["text"] == "Contact"
    assert first["bbox"] == {"x": 0, "y": 5, "width": 9, "height": 9}
    assert first["confidence"] == 95.0
