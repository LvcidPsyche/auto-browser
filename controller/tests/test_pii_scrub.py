from __future__ import annotations

import io
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.pii_scrub import (
    ALL_PATTERN_NAMES,
    PiiScrubber,
    scrub_console_messages,
    scrub_network_body,
    scrub_screenshot,
    scrub_text,
)


def tmp_png(raw: bytes) -> Path:
    """Write PNG bytes to a temp file and return its path."""
    handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    try:
        handle.write(raw)
    finally:
        handle.close()
    return Path(handle.name)


def _settings(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "pii_scrub_enabled": True,
        "pii_scrub_screenshot": True,
        "pii_scrub_network": True,
        "pii_scrub_console": True,
        "pii_scrub_replacement": "[REDACTED]",
        "pii_scrub_audit_report": True,
        "pii_scrub_patterns": "",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_phone_us_no_false_positive_version() -> None:
    result = scrub_text("python 3.11.0 installed")
    assert "[REDACTED]" not in result.text


def test_phone_us_matches_real_number() -> None:
    result = scrub_text("call me at 555-867-5309 please")
    assert "[REDACTED]" in result.text


def test_default_settings_disable_generic_hex_token() -> None:
    scrubber = PiiScrubber.from_settings(_settings())

    assert scrubber.enabled_patterns is not None
    assert "generic_hex_token" not in scrubber.enabled_patterns


def test_explicit_patterns_can_reenable_generic_hex_token() -> None:
    scrubber = PiiScrubber.from_settings(_settings(pii_scrub_patterns="generic_hex_token,email"))

    assert scrubber.enabled_patterns == {"generic_hex_token", "email"}


def test_credit_card_uses_luhn_validation() -> None:
    invalid = scrub_text("card 4111111111111112", enabled_patterns={"credit_card"})
    valid = scrub_text("card 4111111111111111", enabled_patterns={"credit_card"})

    assert invalid.text.endswith("4111111111111112")
    assert valid.text == "card [REDACTED]"


def test_network_body_scrubs_text_and_leaves_binary_unchanged() -> None:
    scrubbed, hits = scrub_network_body(
        b'{"password":"secret-value"}',
        "application/json",
        enabled_patterns={"password_field"},
    )
    unchanged, no_hits = scrub_network_body(b"\x00\x01secret", "application/octet-stream")

    assert scrubbed == b"{[REDACTED]}"
    assert hits[0]["pattern"] == "password_field"
    assert unchanged == b"\x00\x01secret"
    assert no_hits == []


def test_console_messages_and_service_layers_respect_disabled_flags() -> None:
    messages, hits = scrub_console_messages(
        [{"text": "email me at ops@example.com", "level": "info"}],
        enabled_patterns={"email"},
    )
    disabled = PiiScrubber(enabled=False)
    network_disabled = PiiScrubber(network_enabled=False)
    console_disabled = PiiScrubber(console_enabled=False)

    assert messages[0]["text"] == "email me at [REDACTED]"
    assert messages[0]["pii_redacted"] is True
    assert hits[0]["pattern"] == "email"
    assert disabled.text("ops@example.com").text == "ops@example.com"
    assert network_disabled.network_body("secret=abcdefghi", "text/plain") == ("secret=abcdefghi", [])
    assert console_disabled.console([{"text": "ops@example.com"}]) == ([{"text": "ops@example.com"}], [])
    report = PiiScrubber().build_audit_report("s1", "console", hits)
    assert report["patterns_triggered"] == ["email"]
    assert PiiScrubber().summary()["enabled"] is True


def test_screenshot_redaction_and_invalid_image_fallback() -> None:
    import io

    image = Image.new("RGB", (8, 8), "white")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    raw = buf.getvalue()
    blocks = [{"x": 1, "y": 1, "width": 4, "height": 4, "text": "ops@example.com"}]

    redacted, hits = scrub_screenshot(raw, blocks, enabled_patterns={"email"})
    fallback, fallback_hits = scrub_screenshot(b"not-a-png", blocks, enabled_patterns={"email"})

    assert redacted != raw
    assert hits[0]["bbox"] == {"x": 1, "y": 1, "width": 4, "height": 4}
    assert fallback == b"not-a-png"
    assert fallback_hits[0]["pattern"] == "email"
    assert scrub_screenshot(raw, [])[1] == []


# ── Effect tests ───────────────────────────────────────────────────────────
# These assert what actually happened to the bytes/pixels, not what the
# scrubber reported. Redaction silently no-op'd in production for a long time
# while returning hits and writing a "pii_redaction / ok" audit event, because
# every test fed a block shape that OCRExtractor never emits.

SECRET_EMAIL = "ops@corp.com"


def _image_with_secret() -> tuple[bytes, tuple[int, int, int, int]]:
    """A white PNG with the secret drawn at a known location."""
    from PIL import ImageDraw

    image = Image.new("RGB", (400, 120), "white")
    ImageDraw.Draw(image).text((20, 40), SECRET_EMAIL, fill="black")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue(), (18, 38, 170, 62)


def _ink(png: bytes, region: tuple[int, int, int, int]) -> int:
    """Count non-white pixels inside region — the only honest redaction check."""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    x0, y0, x1, y1 = region
    return sum(1 for x in range(x0, x1) for y in range(y0, y1) if image.getpixel((x, y)) != (255, 255, 255))


def _fake_tesseract_data(text: str, *, x: int, y: int, width: int, height: int) -> dict[str, list]:
    """The dict shape pytesseract.image_to_data(output_type=DICT) returns."""
    return {
        "text": [text],
        "conf": ["96.0"],
        "left": [x],
        "top": [y],
        "width": [width],
        "height": [height],
    }


def test_redaction_covers_pixels_for_real_ocr_block_shape(monkeypatch) -> None:
    """Blocks produced by the real OCRExtractor must actually redact pixels.

    Regression: OCRExtractor nests geometry under "bbox" while scrub_screenshot
    read flat x/y/width/height, so every rectangle collapsed to (0,0,0,0). This
    drives the genuine _extract_sync code path (tesseract itself is stubbed) so
    the two modules can never drift apart again without failing here.
    """
    from app import ocr as ocr_module

    raw, region = _image_with_secret()
    monkeypatch.setattr(
        ocr_module.pytesseract,
        "image_to_data",
        lambda *a, **k: _fake_tesseract_data(SECRET_EMAIL, x=20, y=40, width=130, height=18),
    )

    extractor = ocr_module.OCRExtractor(enabled=True, language="eng", max_blocks=20, text_limit=2000)
    payload = extractor._extract_sync(tmp_png(raw))

    blocks = payload["redaction_blocks"]
    assert blocks and "bbox" in blocks[0], "OCR block shape changed; scrub_screenshot must follow"

    before = _ink(raw, region)
    redacted, hits = scrub_screenshot(raw, blocks, enabled_patterns={"email"})
    after = _ink(redacted, region)

    assert hits, "the email should be detected"
    assert before > 0, "fixture should actually draw the secret"
    assert after != before, "reported a redaction but no pixel changed — the silent no-op is back"


def test_degenerate_block_does_not_report_a_redaction() -> None:
    """A zero-size box redacts nothing, so it must not be reported as success."""
    raw, _ = _image_with_secret()
    blocks = [{"text": SECRET_EMAIL, "bbox": {"x": 0, "y": 0, "width": 0, "height": 0}}]
    redacted, hits = scrub_screenshot(raw, blocks, enabled_patterns={"email"})
    assert hits == []
    assert redacted == raw


def test_redaction_is_not_starved_by_the_model_facing_block_cap(monkeypatch) -> None:
    """ocr_max_blocks truncates the payload, never the redaction input."""
    from app import ocr as ocr_module

    words = [f"w{i}" for i in range(50)] + [SECRET_EMAIL]
    monkeypatch.setattr(
        ocr_module.pytesseract,
        "image_to_data",
        lambda *a, **k: {
            "text": words,
            "conf": ["96.0"] * len(words),
            "left": [10] * len(words),
            "top": [10] * len(words),
            "width": [20] * len(words),
            "height": [10] * len(words),
        },
    )
    raw, _ = _image_with_secret()
    extractor = ocr_module.OCRExtractor(enabled=True, language="eng", max_blocks=20, text_limit=2000)
    payload = extractor._extract_sync(tmp_png(raw))

    assert len(payload["blocks"]) == 20, "model-facing payload stays capped"
    assert len(payload["redaction_blocks"]) == len(words), "redaction sees every block"
    assert any(b["text"] == SECRET_EMAIL for b in payload["redaction_blocks"])
    assert not any(b["text"] == SECRET_EMAIL for b in payload["blocks"])


# ── Pattern coverage ───────────────────────────────────────────────────────
# Twelve of the fifteen patterns had no behavioural assertion; line coverage
# read 91% only because scrub_text loops them all regardless. That is how a
# 19-character AWS pattern (real keys are 20) survived.

CANONICAL_SAMPLES: dict[str, str] = {
    "aws_access_key": "key=AKIAIOSFODNN7EXAMPLE",
    "aws_secret_key": "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "jwt_token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    "bearer_token": "Authorization: Bearer abc123XYZdef456==",
    "pem_header": "-----BEGIN RSA PRIVATE KEY-----",
    "api_key_param": "api_key=sk_live_abcdefgh12345678",
    "password_field": '"password": "hunter2xyz"',
    "credit_card": "4111111111111111",
    "ssn": "123-45-6789",
    "email": "ops@example.com",
    "phone_us": "415-555-0132",
    "phone_intl": "+44 20 7946 0958",
    "gcp_sa_key": '"private_key_id": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"',
    "azure_secret": "client_secret=0123456789abcdef0123456789abcdef",
    "generic_hex_token": "0123456789abcdef0123456789abcdef",
    "generic_b64_secret": "credential=QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWY=",
}


def test_every_pattern_has_a_canonical_sample() -> None:
    """Adding a pattern without a behavioural sample fails here, by design."""
    assert set(CANONICAL_SAMPLES) == set(ALL_PATTERN_NAMES)


@pytest.mark.parametrize("name", sorted(CANONICAL_SAMPLES))
def test_pattern_redacts_its_canonical_sample(name: str) -> None:
    sample = CANONICAL_SAMPLES[name]
    result = scrub_text(sample, enabled_patterns={name})
    assert result.scrubbed, f"{name} did not match its canonical sample: {sample!r}"
    assert "[REDACTED]" in result.text


def test_aws_access_key_matches_a_full_length_key() -> None:
    """Regression: the pattern was 19 chars (A + 2 + 16); real key ids are 20."""
    result = scrub_text("key=AKIAIOSFODNN7EXAMPLE", enabled_patterns={"aws_access_key"})
    assert "AKIAIOSFODNN7EXAMPLE" not in result.text


def test_bearer_token_redaction_consumes_base64_padding() -> None:
    """Regression: a trailing \\b cannot match after '=', leaking the padding."""
    result = scrub_text("Bearer abcdef123==", enabled_patterns={"bearer_token"})
    assert "=" not in result.text


# ── Configuration must fail closed ─────────────────────────────────────────


def test_unknown_pattern_name_is_rejected() -> None:
    """A typo used to empty the set, silently disabling ALL PII scrubbing while
    summary() still reported enabled: True."""
    with pytest.raises(ValueError) as excinfo:
        PiiScrubber.from_settings(_settings(pii_scrub_patterns="emial,phon"))
    assert "emial" in str(excinfo.value)


def test_valid_pattern_subset_still_configures() -> None:
    scrubber = PiiScrubber.from_settings(_settings(pii_scrub_patterns="email,ssn"))
    assert scrubber.enabled_patterns == {"email", "ssn"}
    assert scrubber.text("reach me at ops@example.com").scrubbed
    assert not scrubber.text("Bearer abcdef123456").scrubbed
