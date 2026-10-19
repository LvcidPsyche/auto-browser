"""A private key is redacted whole, not just its BEGIN line.

`pem_header` used to match only the `-----BEGIN ... KEY-----` line, so the
base64 body (the key itself) and the END line survived in text, network
bodies and console messages.
"""

from __future__ import annotations

import pytest

from app.pii_scrub import PiiScrubber, scrub_network_body, scrub_text

BODY_LINES = (
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
    "BKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj",
    "MzEfYyjiWA4R4/M2bS1GB4t7NXp98C3S",
)
LABELS = (
    "PRIVATE KEY",
    "RSA PRIVATE KEY",
    "EC PRIVATE KEY",
    "DSA PRIVATE KEY",
    "OPENSSH PRIVATE KEY",
    "ENCRYPTED PRIVATE KEY",
)


def _pem(label: str, newline: str = "\n", *, end: bool = True) -> str:
    lines = [f"-----BEGIN {label}-----", *BODY_LINES]
    if end:
        lines.append(f"-----END {label}-----")
    return newline.join(lines)


def _assert_no_key_material(text: str) -> None:
    for line in BODY_LINES:
        assert line not in text
    assert "-----END" not in text


@pytest.mark.parametrize("label", LABELS)
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_whole_private_key_block_is_redacted(label: str, newline: str) -> None:
    result = scrub_text(f"before{newline}{_pem(label, newline)}{newline}after", enabled_patterns={"pem_header"})

    assert result.text == f"before{newline}[REDACTED]{newline}after"
    assert [hit["pattern"] for hit in result.hits] == ["pem_header"]


def test_missing_end_line_redacts_to_the_end_of_the_text() -> None:
    result = scrub_text("log: " + _pem("RSA PRIVATE KEY", end=False), enabled_patterns={"pem_header"})

    assert result.text == "log: [REDACTED]"


def test_two_keys_in_one_text_are_both_redacted() -> None:
    text = f"a {_pem('PRIVATE KEY')} b {_pem('EC PRIVATE KEY')} c"
    result = scrub_text(text, enabled_patterns={"pem_header"})

    assert result.text == "a [REDACTED] b [REDACTED] c"


def test_key_inside_a_json_string_is_redacted() -> None:
    # JSON carries a newline as the two characters backslash + n, not as a line break.
    nl = chr(92) + "n"
    body = '{"key": "-----BEGIN PRIVATE KEY-----' + nl + "MIIEvQIBADANBgkqhkiG9w0BAQEF" + nl
    body += "-----END PRIVATE KEY-----" + nl + '", "n": 1}'
    scrubbed, hits = scrub_network_body(body, "application/json", enabled_patterns={"pem_header"})

    assert scrubbed == '{"key": "[REDACTED]' + nl + '", "n": 1}'
    assert hits


def test_public_key_is_left_alone() -> None:
    """A public key is not a secret; redacting only its header corrupted it for no gain."""
    text = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE\n-----END PUBLIC KEY-----"

    assert scrub_text(text, enabled_patterns={"pem_header"}).text == text


def test_text_channel_through_the_scrubber() -> None:
    scrubber = PiiScrubber(enabled_patterns={"pem_header"})

    _assert_no_key_material(scrubber.text(_pem("OPENSSH PRIVATE KEY", "\r\n")).text)


def test_network_channel_through_the_scrubber() -> None:
    scrubber = PiiScrubber(enabled_patterns={"pem_header"})
    for body in (_pem("PRIVATE KEY"), _pem("RSA PRIVATE KEY", "\r\n").encode()):
        scrubbed, hits = scrubber.network_body(body, "text/plain")
        assert hits
        _assert_no_key_material(scrubbed.decode() if isinstance(scrubbed, bytes) else scrubbed)


def test_console_channel_through_the_scrubber() -> None:
    scrubber = PiiScrubber(enabled_patterns={"pem_header"})
    messages = [{"type": "log", "text": _pem("EC PRIVATE KEY", "\r\n")}, {"type": "log", "text": "plain"}]

    cleaned, hits = scrubber.console(messages)

    assert hits
    _assert_no_key_material(cleaned[0]["text"])
    assert cleaned[0]["pii_redacted"] is True
    assert cleaned[1] == {"type": "log", "text": "plain"}
