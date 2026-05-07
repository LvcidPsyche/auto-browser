from __future__ import annotations

from types import SimpleNamespace

from PIL import Image

from app.pii_scrub import (
    PiiScrubber,
    scrub_console_messages,
    scrub_network_body,
    scrub_screenshot,
    scrub_text,
)


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
