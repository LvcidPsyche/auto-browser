"""
pii_scrub.py — Multi-layer PII scrubbing for auto-browser.

Layers applied across the session lifecycle:
  1. Text scrubbing    — regex patterns over any string value
  2. Screenshot redact — black pixel boxes drawn over OCR-detected PII regions
  3. Network payloads  — request/response bodies scrubbed before storage
  4. Console logs      — scrubbed before returning to caller
  5. Audit metadata    — sensitive fields masked before writing to audit log

Patterns covered:
  Email addresses, US/intl phone numbers, SSNs, credit card numbers,
  AWS/GCP/Azure API keys, JWT tokens, Bearer tokens, private key PEM headers,
  generic api_key/token/secret/password URL/query params, IP addresses in
  credentials context, base64-encoded JSON credential blobs.

Config (via Settings):
  PII_SCRUB_ENABLED          — master switch (default: True)
  PII_SCRUB_SCREENSHOT       — pixel-redact screenshots (default: True)
  PII_SCRUB_NETWORK          — scrub network payloads (default: True)
  PII_SCRUB_CONSOLE          — scrub console log text (default: True)
  PII_SCRUB_PATTERNS         — comma-separated pattern names to enable
                               (default: all)
  PII_SCRUB_REPLACEMENT      — replacement string (default: [REDACTED])
  PII_SCRUB_AUDIT_REPORT     — emit redaction events to audit log (default: True)
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Replacement sentinel ───────────────────────────────────────────────────
DEFAULT_REPLACEMENT = "[REDACTED]"

# ── Pattern definitions ────────────────────────────────────────────────────
# Each entry: (name, compiled_regex)
# Patterns are ordered from most-specific to least-specific to avoid
# double-redaction artifacts.

_RAW_PATTERNS: list[tuple[str, str]] = [
    # AWS access key IDs: AKIA / ASIA / AROA / AIDA / AIPA / ANPA / ANVA / APKA
    # The prefix is 4 characters and the identifier is 20 total. The previous
    # pattern spelled the prefix as `A[KSIARP][IDA]` (3 chars) + 16, i.e. 19 —
    # one short — so the trailing lookahead rejected every real key and this
    # pattern could never match anything. Spell the prefixes out instead.
    (
        "aws_access_key",
        r"(?<![A-Z0-9])((?:AKIA|ASIA|AROA|AIDA|AIPA|ANPA|ANVA|APKA)[A-Z0-9]{16})(?![A-Z0-9])",
    ),
    # AWS secret access keys: 40 chars base64url after = sign or whitespace
    ("aws_secret_key", r"(?i)(?:aws_secret(?:_access)?_key|secret_access_key)\s*[=:]\s*([A-Za-z0-9/+=]{40})"),
    # JWT tokens: header.payload.signature (base64url)
    ("jwt_token", r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
    # Bearer tokens in Authorization headers.
    # No trailing \b: a word boundary cannot occur after '=', so it forced the
    # engine to backtrack off the base64 padding and leave it in the output
    # ("Bearer abc123==" redacted to "[REDACTED]==").
    ("bearer_token", r"(?i)Bearer\s+([A-Za-z0-9\-._~+/]+=*)"),
    # Private / public key PEM headers
    ("pem_header", r"-----BEGIN\s+(?:RSA\s+)?(?:PRIVATE|PUBLIC)\s+KEY-----"),
    # Generic API key / token / secret / password in query params or JSON fields
    (
        "api_key_param",
        r"(?i)(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|"
        r"client[_-]?secret|app[_-]?secret|secret[_-]?key|private[_-]?key|"
        r"x-api-key|access_key|refresh_token|id_token|session_token|"
        r"auth_secret|webhook_secret|signing_secret|consumer_secret|"
        r"oauth_token|oauth_secret)"
        r'\s*[=:]\s*["\']?([A-Za-z0-9\-._~+/!@#$%^&*()]{8,})["\']?',
    ),
    # Password fields in JSON or form data
    (
        "password_field",
        r'(?i)"?(?:password|passwd|pwd|pass|new_password|current_password|'
        r'confirm_password)"?\s*[=:]\s*["\']?([^\s"\'&]{4,})["\']?',
    ),
    # Credit card numbers: 13-19 digit sequences with optional spaces/dashes
    # Very rough — checked with Luhn in code, not regex
    (
        "credit_card",
        r"\b(?:4[0-9]{12}(?:[0-9]{3,6})?|5[1-5][0-9]{14}|"
        r"3[47][0-9]{13}|3(?:0[0-5]|[68][0-9])[0-9]{11}|"
        r"6(?:011|5[0-9]{2})[0-9]{12}|(?:2131|1800|35\d{3})\d{11})\b",
    ),
    # US SSN: ###-##-####
    ("ssn", r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    # Email addresses
    (
        "email",
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
    ),
    # US phone numbers: many formats
    (
        "phone_us",
        r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}(?!\d)",
    ),
    # International phone numbers starting with +
    ("phone_intl", r"\+(?:[0-9] ?){6,14}[0-9]\b"),
    # GCP service account JSON keys (partial match via "private_key_id")
    ("gcp_sa_key", r'"private_key_id"\s*:\s*"([a-f0-9]{40})"'),
    # Azure / generic client secrets (long hex strings after known labels)
    (
        "azure_secret",
        r"(?i)(?:client_secret|tenant_id|application_id)\s*[=:]\s*"
        r'["\']?([a-f0-9\-]{32,})["\']?',
    ),
    # Generic hex API tokens 32+ chars (last resort, low-specificity)
    ("generic_hex_token", r"\b[a-f0-9]{32,64}\b"),
    # Generic base64 secrets > 30 chars that look like credentials
    # (appears after = in assignments / JSON)
    (
        "generic_b64_secret",
        r"(?i)(?:secret|token|key|credential|cred)\s*[=:]\s*"
        r'"?([A-Za-z0-9+/]{30,}={0,2})"?',
    ),
]

_COMPILED: dict[str, re.Pattern[str]] = {name: re.compile(pattern) for name, pattern in _RAW_PATTERNS}

# Pattern display order (for reports)
ALL_PATTERN_NAMES = [name for name, _ in _RAW_PATTERNS]
_NOISY_PATTERNS = {"generic_hex_token"}


# ── Luhn check for credit card validation ─────────────────────────────────


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


# ── Core text scrubber ─────────────────────────────────────────────────────


@dataclass
class ScrubResult:
    text: str
    hits: list[dict[str, Any]] = field(default_factory=list)

    @property
    def scrubbed(self) -> bool:
        return bool(self.hits)


def scrub_text(
    text: str,
    replacement: str = DEFAULT_REPLACEMENT,
    enabled_patterns: set[str] | None = None,
) -> ScrubResult:
    """
    Apply all PII patterns to `text` and return a ScrubResult.

    `enabled_patterns` can restrict which pattern names are applied.
    If None, all patterns are active.
    """
    if not text:
        return ScrubResult(text=text)

    hits: list[dict[str, Any]] = []
    result = text

    for name, pattern in _COMPILED.items():
        if enabled_patterns is not None and name not in enabled_patterns:
            continue

        def _replace(m: re.Match, _name: str = name) -> str:  # noqa: B023
            matched = m.group(0)
            # Extra validation for credit cards
            if _name == "credit_card":
                digits_only = re.sub(r"[\s-]", "", matched)
                if not _luhn_check(digits_only):
                    return matched
            hits.append(
                {
                    "pattern": _name,
                    "offset": m.start(),
                    "length": len(matched),
                    "preview": matched[:6] + "…" if len(matched) > 6 else "…",
                }
            )
            return replacement

        result = pattern.sub(_replace, result)

    return ScrubResult(text=result, hits=hits)


# ── Screenshot pixel redaction ─────────────────────────────────────────────


def scrub_screenshot(
    image_bytes: bytes,
    ocr_blocks: list[dict[str, Any]],
    replacement: str = DEFAULT_REPLACEMENT,
    enabled_patterns: set[str] | None = None,
) -> tuple[bytes, list[dict[str, Any]]]:
    """
    Redact PII from a PNG screenshot by drawing opaque black boxes
    over OCR bounding boxes that contain PII text.

    Args:
        image_bytes: Raw PNG bytes of the screenshot.
        ocr_blocks:  List of OCR result dicts with keys:
                     x, y, width, height, text (from ocr.py OCRExtractor)
        replacement: Replacement string for PII found in OCR text.
        enabled_patterns: Restrict to these pattern names (None = all).

    Returns:
        (redacted_png_bytes, list_of_hit_records)
    """
    if not ocr_blocks:
        return image_bytes, []

    all_hits: list[dict[str, Any]] = []
    boxes_to_redact: list[tuple[int, int, int, int]] = []

    for block in ocr_blocks:
        text = block.get("text", "")
        if not text:
            continue
        result = scrub_text(text, replacement=replacement, enabled_patterns=enabled_patterns)
        if result.scrubbed:
            # OCRExtractor emits geometry nested under "bbox" (see ocr.py); this
            # read them as flat top-level keys, so every .get(..., 0) fell to its
            # default and every rectangle computed to (0, 0, 0, 0). Redaction was
            # a silent no-op in production while `hits` stayed non-empty, so the
            # caller wrote a "pii_redaction / ok" audit event over an unredacted
            # screenshot. The flat shape is still accepted for callers that pass
            # geometry directly.
            geometry = block.get("bbox") or block
            x = int(geometry.get("x", 0))
            y = int(geometry.get("y", 0))
            w = int(geometry.get("width", 0))
            h = int(geometry.get("height", 0))
            if w <= 0 or h <= 0:
                # A degenerate box redacts nothing. Refuse to report success for
                # it — that is exactly how this defect stayed invisible.
                logger.warning(
                    "pii_scrub: OCR block matched %s but carries no usable geometry; cannot redact pixels",
                    [h["pattern"] for h in result.hits],
                )
                continue
            boxes_to_redact.append((x, y, x + w, y + h))
            for hit in result.hits:
                hit["bbox"] = {"x": x, "y": y, "width": w, "height": h}
                all_hits.append(hit)

    if not boxes_to_redact:
        return image_bytes, []

    try:
        from PIL import Image, ImageDraw  # lazy import — Pillow is in requirements

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        for box in boxes_to_redact:
            draw.rectangle(box, fill=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), all_hits
    except Exception as exc:
        logger.warning("screenshot PII redaction failed: %s", exc)
        return image_bytes, all_hits


# ── Network payload scrubber ───────────────────────────────────────────────


def scrub_network_body(
    body: str | bytes | None,
    content_type: str = "",
    replacement: str = DEFAULT_REPLACEMENT,
    enabled_patterns: set[str] | None = None,
) -> tuple[str | bytes | None, list[dict[str, Any]]]:
    """
    Scrub PII from a network request/response body.

    Only processes text-like content types (JSON, form, HTML, plain text).
    Binary content is returned unchanged.
    """
    if body is None:
        return body, []

    ct = content_type.lower()
    is_text = any(marker in ct for marker in ("json", "text", "form", "xml", "javascript", "graphql"))
    if not is_text:
        return body, []

    try:
        text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    except Exception:
        return body, []

    result = scrub_text(text, replacement=replacement, enabled_patterns=enabled_patterns)
    if not result.scrubbed:
        return body, []

    scrubbed = result.text.encode("utf-8") if isinstance(body, bytes) else result.text
    return scrubbed, result.hits


# ── Console log scrubber ───────────────────────────────────────────────────


def scrub_console_messages(
    messages: list[dict[str, Any]],
    replacement: str = DEFAULT_REPLACEMENT,
    enabled_patterns: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Scrub PII from console message text fields.

    Returns (scrubbed_messages, all_hits).
    """
    all_hits: list[dict[str, Any]] = []
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        text = msg.get("text", "")
        result = scrub_text(text, replacement=replacement, enabled_patterns=enabled_patterns)
        entry = dict(msg)
        if result.scrubbed:
            entry["text"] = result.text
            entry["pii_redacted"] = True
            all_hits.extend(result.hits)
        cleaned.append(entry)
    return cleaned, all_hits


# ── Scrubber service (stateful, configured once) ───────────────────────────


class PiiScrubber:
    """
    Stateful PII scrubber that applies all configured layers.

    Instantiated once from settings and injected into BrowserManager.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        screenshot_enabled: bool = True,
        network_enabled: bool = True,
        console_enabled: bool = True,
        replacement: str = DEFAULT_REPLACEMENT,
        enabled_patterns: set[str] | None = None,
        audit_report: bool = True,
    ):
        self.enabled = enabled
        self.screenshot_enabled = screenshot_enabled and enabled
        self.network_enabled = network_enabled and enabled
        self.console_enabled = console_enabled and enabled
        self.replacement = replacement
        self.enabled_patterns = enabled_patterns  # None = all patterns active
        self.audit_report = audit_report

    @classmethod
    def from_settings(cls, settings: Any) -> "PiiScrubber":
        """Build from auto-browser Settings object."""
        enabled = getattr(settings, "pii_scrub_enabled", True)
        screenshot = getattr(settings, "pii_scrub_screenshot", True)
        network = getattr(settings, "pii_scrub_network", True)
        console = getattr(settings, "pii_scrub_console", True)
        replacement = getattr(settings, "pii_scrub_replacement", DEFAULT_REPLACEMENT)
        audit = getattr(settings, "pii_scrub_audit_report", True)

        raw_patterns = getattr(settings, "pii_scrub_patterns", "")
        patterns: set[str] | None = None
        if raw_patterns:
            patterns = {p.strip() for p in raw_patterns.split(",") if p.strip()}
            # Fail closed on an unknown name. This previously intersected with the
            # known set and silently dropped anything unrecognised, so a single
            # typo ("emial") could reduce the set to empty — and an empty (but not
            # None) set makes scrub_text skip every pattern, disabling PII
            # scrubbing entirely while summary() still reported enabled: True.
            unknown = sorted(patterns - set(ALL_PATTERN_NAMES))
            if unknown:
                raise ValueError(
                    f"PII_SCRUB_PATTERNS contains unknown pattern name(s): {', '.join(unknown)}. "
                    f"Valid names: {', '.join(sorted(ALL_PATTERN_NAMES))}"
                )
        else:
            patterns = set(ALL_PATTERN_NAMES) - _NOISY_PATTERNS

        return cls(
            enabled=enabled,
            screenshot_enabled=screenshot,
            network_enabled=network,
            console_enabled=console,
            replacement=replacement,
            enabled_patterns=patterns,
            audit_report=audit,
        )

    # ── Public API ──────────────────────────────────────────────────────────

    def text(self, value: str) -> ScrubResult:
        """Scrub a text string. Returns ScrubResult(text, hits)."""
        if not self.enabled:
            return ScrubResult(text=value)
        return scrub_text(value, self.replacement, self.enabled_patterns)

    def screenshot(self, image_bytes: bytes, ocr_blocks: list[dict[str, Any]]) -> tuple[bytes, list[dict[str, Any]]]:
        """Pixel-redact PII in a PNG screenshot using OCR bounding boxes."""
        if not self.screenshot_enabled:
            return image_bytes, []
        return scrub_screenshot(image_bytes, ocr_blocks, self.replacement, self.enabled_patterns)

    def network_body(
        self, body: str | bytes | None, content_type: str = ""
    ) -> tuple[str | bytes | None, list[dict[str, Any]]]:
        """Scrub a network request/response body."""
        if not self.network_enabled:
            return body, []
        return scrub_network_body(body, content_type, self.replacement, self.enabled_patterns)

    def console(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Scrub PII from console log messages."""
        if not self.console_enabled:
            return messages, []
        return scrub_console_messages(messages, self.replacement, self.enabled_patterns)

    def build_audit_report(
        self,
        session_id: str,
        layer: str,
        hits: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build an audit event dict for PII redaction."""
        return {
            "event": "pii_redaction",
            "session_id": session_id,
            "layer": layer,
            "hit_count": len(hits),
            "patterns_triggered": list({h["pattern"] for h in hits}),
        }

    def summary(self) -> dict[str, Any]:
        """Return scrubber configuration summary (for /healthz or debug)."""
        return {
            "enabled": self.enabled,
            "screenshot": self.screenshot_enabled,
            "network": self.network_enabled,
            "console": self.console_enabled,
            "patterns": (list(self.enabled_patterns) if self.enabled_patterns is not None else ALL_PATTERN_NAMES),
            "replacement": self.replacement,
            "audit_report": self.audit_report,
        }
