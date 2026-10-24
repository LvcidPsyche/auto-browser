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
  AWS/GCP/Azure API keys, JWT tokens, Bearer tokens, private key PEM blocks,
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
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .pii_cards import CARD_SHAPE, card_spans
from .pii_secret_fields import SECRET_NAME_HINT, form_secret_values, json_secret_values
from .pii_views import (
    Found,
    Span,
    decode_form,
    decode_json_escapes,
    decode_json_token_escapes,
    inside_any,
    merge_found,
    merge_spans,
    replace_spans,
)

logger = logging.getLogger(__name__)

# ── Replacement sentinel ───────────────────────────────────────────────────
DEFAULT_REPLACEMENT = "[REDACTED]"

# ── Pattern definitions ────────────────────────────────────────────────────
# Each entry: (name, compiled_regex)
# Patterns are ordered from most-specific to least-specific to avoid
# double-redaction artifacts.

_API_KEY_VALUE_CHARS = r"A-Za-z0-9\-._~+/!@#$%^&*()"


def _api_key_param(value_chars: str) -> str:
    return (
        r"(?i)(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|"
        r"client[_-]?secret|app[_-]?secret|secret[_-]?key|private[_-]?key|"
        r"x-api-key|access_key|refresh_token|id_token|session_token|"
        r"auth_secret|webhook_secret|signing_secret|consumer_secret|"
        r"oauth_token|oauth_secret)"
        r'\s*[=:]\s*["\']?([' + value_chars + r']{8,})["\']?'
    )


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
    # JWT tokens: header.payload.signature (base64url). The lookbehind lets a
    # match start only where a token starts: "eyJeyJeyJ…" otherwise made every
    # "eyJ" rescan the rest of the run, quadratic in the input.
    ("jwt_token", r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
    # Provider API tokens, by their documented prefixes. Anchored to a token
    # start and long enough that ordinary hyphenated words do not match.
    (
        "provider_token",
        r"(?<![A-Za-z0-9_-])(?:"
        r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{40,}|sk-ant-[a-z]+\d{2}-[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9]{32,}|"
        r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}|xox[abposr]-[A-Za-z0-9-]{10,}|"
        r"AIza[0-9A-Za-z_-]{35}|glpat-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{30,})",
    ),
    # HTTP Basic credentials, only after an Authorization header name: "Basic"
    # alone is an ordinary word.
    ("basic_auth", r"(?i)\b(?:proxy-)?authorization\s*[:=]\s*[\"']?basic\s+[A-Za-z0-9+/]{4,}={0,2}"),
    # Cookie headers as text (console logs, bodies); captured headers are
    # masked separately. Needs name=value after the colon.
    ("cookie_header", r"(?i)(?<![A-Za-z-])(?:set-)?cookie\s*:\s*[^\s=;,]+=[^\r\n]*"),
    # OAuth authorization codes in callback URLs.
    ("oauth_code", r"(?<=[?&#])code=[A-Za-z0-9._~%/+-]{12,}"),
    # Bearer tokens in Authorization headers.
    # No trailing \b: a word boundary cannot occur after '=', so it forced the
    # engine to backtrack off the base64 padding and leave it in the output
    # ("Bearer abc123==" redacted to "[REDACTED]==").
    ("bearer_token", r"(?i)Bearer\s+([A-Za-z0-9\-._~+/]+=*)"),
    # A private key PEM block, BEGIN line to the matching END line. The name is
    # kept because PII_SCRUB_PATTERNS users list it; it used to match only the
    # BEGIN line, leaving the base64 body (the key) and the END line in the
    # output. Any label ending in "PRIVATE KEY" (RSA, EC, DSA, OPENSSH,
    # ENCRYPTED, PGP ... BLOCK) counts. Without an END line (a truncated body,
    # a console line cut off) everything to the end of the text goes: failing
    # closed beats leaving half a key. PUBLIC KEY blocks are not secret, so they
    # are no longer touched. `\s` spans CRLF, and the lazy body is linear.
    (
        "pem_header",
        r"-----BEGIN\s+((?:[A-Z0-9]+\s+){0,3}PRIVATE\s+KEY(?:\s+BLOCK)?)-----"
        r"(?:(?s:.*?)-----END\s+\1-----|(?s:.*))",
    ),
    # Generic API key / token / secret / password in query params or JSON fields
    ("api_key_param", _api_key_param(_API_KEY_VALUE_CHARS)),
    # Password fields in JSON or form data
    (
        "password_field",
        r'(?i)"?(?:password|passwd|pwd|pass|new_password|current_password|'
        r'confirm_password)"?\s*[=:]\s*["\']?([^\s"\'&]{4,})["\']?',
    ),
    # Credit card numbers: 13-19 digits, with or without space/dash grouping.
    # The regex only finds the shape; pii_cards.card_spans checks the issuer
    # prefix and Luhn, and finds the card in a run of digit groups. The previous
    # pattern promised separators in its comment but matched contiguous digits
    # only, so "4111 1111 1111 1111" — how a card number is usually shown — was
    # never redacted.
    ("credit_card", CARD_SHAPE),
    # US SSN: ###-##-####
    ("ssn", r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    # Email addresses. The local part starts where a run of local-part
    # characters starts; with `\b` there it could start inside a run, and text
    # with a boundary at every character ("a.a.a.…") was rescanned to its end
    # from each one: quadratic, 3.4 s for 40 KB of page-controlled console text.
    (
        "email",
        r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
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

# The patterns as a form or a query reads them. An api_key_param value runs on through a literal `&`, but
# there `&` starts the next parameter (a `&` inside a value is written `%26`), and the match used to
# take the next name with it: `api_key=abcdefgh&password=x` left `[REDACTED]=x`.
_FORM_COMPILED = {**_COMPILED, "api_key_param": re.compile(_api_key_param(_API_KEY_VALUE_CHARS.replace("&", "")))}

# In a form or a query these two redact the value and keep the name, as the pass by name does: the name
# stays a name for the next pass to read, and the stored form still says which field it was.
_FORM_VALUE_ONLY = frozenset({"api_key_param", "password_field"})

# In JSON these five redact the value and nothing around it. Their matches take an optional quote after
# the value (and password_field and api_key_param a name with its quotes before it, gcp_sa_key its own
# name and both quotes), which in a JSON string is the closing quote: whole-match redaction left
# `"next":"...?[REDACTED],"n":1`, no longer JSON.
_JSON_VALUE_ONLY = frozenset({"api_key_param", "password_field", "azure_secret", "generic_b64_secret", "gcp_sa_key"})

# Pattern display order (for reports)
ALL_PATTERN_NAMES = [name for name, _ in _RAW_PATTERNS]
_NOISY_PATTERNS = {"generic_hex_token"}


def _part_to_redact(name: str, match: re.Match[str], value_only: frozenset[str]) -> Span:
    """The stretch of a pattern's match that is to go, as positions in the text matched."""
    if name in value_only:
        return match.span(1)
    return match.span()


def find_pii_spans(
    text: str, enabled_patterns: set[str] | None = None, form: bool = False, json: bool = False
) -> list[tuple[int, int, str]]:
    """(start, end, pattern) of every PII match in `text`, as positions in `text` itself.

    scrub_text rewrites as it goes, so the offsets it reports belong to a
    partly redacted string; redacting pixels needs positions in the original.
    `form` reads `text` as a form or a query (see `_FORM_COMPILED`), `json` as JSON, where five
    patterns redact only their value (see `_JSON_VALUE_ONLY`).
    """
    value_only = _FORM_VALUE_ONLY if form else _JSON_VALUE_ONLY if json else frozenset()
    spans: list[tuple[int, int, str]] = []
    for name, pattern in (_FORM_COMPILED if form else _COMPILED).items():
        if enabled_patterns is not None and name not in enabled_patterns:
            continue
        if name == "credit_card":
            spans += [(start, end, name) for start, end in card_spans(text)]
            continue
        for match in pattern.finditer(text):
            spans.append((*_part_to_redact(name, match, value_only), name))
    return spans


# ── Core text scrubber ─────────────────────────────────────────────────────


@dataclass
class ScrubResult:
    text: str
    hits: list[dict[str, Any]] = field(default_factory=list)

    @property
    def scrubbed(self) -> bool:
        return bool(self.hits)


def _hit_record(
    pattern: str, offset: int, matched: str, value_only: frozenset[str] = _FORM_VALUE_ONLY
) -> dict[str, Any]:
    # A match of these is the value itself, so they show no preview, wherever they match (in text, the
    # two of a form; in JSON, the five of `_JSON_VALUE_ONLY`).
    secret = pattern in value_only
    return {
        "pattern": pattern,
        "offset": offset,
        "length": len(matched),
        "preview": matched[:6] + "…" if len(matched) > 6 and not secret else "…",
    }


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
    return _scrub_with(text, replacement, enabled_patterns, form=False)


def _scrub_with(text: str, replacement: str, enabled_patterns: set[str] | None, form: bool) -> ScrubResult:
    """scrub_text, reading `text` as a form or a query when `form` is set (see `_FORM_COMPILED`)."""
    if not text:
        return ScrubResult(text=text)

    hits: list[dict[str, Any]] = []
    result = text
    encoded = urllib.parse.quote_plus(replacement)
    value_only = _FORM_VALUE_ONLY if form else frozenset()

    for name, pattern in (_FORM_COMPILED if form else _COMPILED).items():
        if enabled_patterns is not None and name not in enabled_patterns:
            continue

        if name == "credit_card":
            spans = card_spans(result)
            hits += [_hit_record(name, start, result[start:end]) for start, end in spans]
            result = replace_spans(result, spans, replacement)
            continue

        def _replace(m: re.Match, _name: str = name) -> str:  # noqa: B023
            span = _part_to_redact(_name, m, value_only)
            matched = m.group(0)
            start, end = span[0] - m.start(), span[1] - m.start()
            hits.append(_hit_record(_name, span[0], matched[start:end]))
            # A value left standing beside its name is written as a form value; the rest are as they were.
            kept = encoded if _name in value_only else replacement
            return matched[:start] + kept + matched[end:]

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
    over the OCR words it is written in.

    Raises whatever Pillow raises when the image cannot be decoded, drawn on or
    encoded, after logging it; the original bytes are never returned alongside
    hits, because that reads as a redaction that happened.

    OCR reports one block per word, and each word used to be scrubbed on its
    own, so nothing containing a space ever matched: "(555) 123-4567", a card
    number in groups. Words are now joined into the line OCR found them on
    (`line` holds tesseract's block/paragraph/line numbers), the line is
    scrubbed, and every word a match touches is redacted. A block without line
    information is a line of its own.

    Args:
        image_bytes: Raw PNG bytes of the screenshot.
        ocr_blocks:  List of OCR result dicts with keys:
                     text, bbox {x, y, width, height}, line (from ocr.py OCRExtractor)
        replacement: Unused; kept for the call signature.
        enabled_patterns: Restrict to these pattern names (None = all).

    Returns:
        (redacted_png_bytes, list_of_hit_records)
    """
    if not ocr_blocks:
        return image_bytes, []

    all_hits: list[dict[str, Any]] = []
    boxes_to_redact: list[tuple[int, int, int, int]] = []

    for words in _ocr_lines(ocr_blocks):
        text, word_spans = _join_words(words)
        for start, end, pattern in find_pii_spans(text, enabled_patterns):
            matched = text[start:end]
            for word, (word_start, word_end) in zip(words, word_spans):
                if word_start >= end or word_end <= start:
                    continue
                box = _word_box(word)
                if box is None:
                    # A degenerate box redacts nothing. Refuse to report success
                    # for it — that is how redaction once stayed a silent no-op.
                    logger.warning(
                        "pii_scrub: OCR block matched %s but carries no usable geometry; cannot redact pixels",
                        pattern,
                    )
                    continue
                x, y, w, h = box
                boxes_to_redact.append((x, y, x + w, y + h))
                all_hits.append(
                    {
                        "pattern": pattern,
                        "offset": start,
                        "length": len(matched),
                        "preview": matched[:6] + "…" if len(matched) > 6 else "…",
                        "bbox": {"x": x, "y": y, "width": w, "height": h},
                    }
                )

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
        # Returning the original bytes with the hits attached would read as a
        # successful redaction to the caller. Let it decide what to report.
        logger.warning("screenshot PII redaction failed: %s", exc)
        raise


def _ocr_lines(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    lines: dict[Any, list[dict[str, Any]]] = {}
    for index, block in enumerate(blocks):
        if not block.get("text"):
            continue
        key = tuple(block["line"]) if block.get("line") else ("block", index)
        lines.setdefault(key, []).append(block)
    return list(lines.values())


def _join_words(words: list[dict[str, Any]]) -> tuple[str, list[tuple[int, int]]]:
    spans: list[tuple[int, int]] = []
    position = 0
    for word in words:
        length = len(str(word["text"]))
        spans.append((position, position + length))
        position += length + 1
    return " ".join(str(word["text"]) for word in words), spans


def _word_box(block: dict[str, Any]) -> tuple[int, int, int, int] | None:
    # OCRExtractor nests geometry under "bbox"; reading it flat once made every
    # rectangle (0, 0, 0, 0). The flat shape is still accepted for callers that
    # pass geometry directly.
    geometry = block.get("bbox") or block
    x, y = int(geometry.get("x", 0)), int(geometry.get("y", 0))
    w, h = int(geometry.get("width", 0)), int(geometry.get("height", 0))
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


# ── Network payload scrubber ───────────────────────────────────────────────


_TEXT_TYPE_MARKERS = ("json", "text", "form", "xml", "javascript", "graphql", "yaml", "html")
_BINARY_MAJOR_TYPES = ("image/", "audio/", "video/", "font/")
_BINARY_MEDIA_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/x-gzip",
        "application/x-tar",
        "application/protobuf",
        "application/x-protobuf",
        "application/vnd.google.protobuf",
        "application/msgpack",
        "application/x-msgpack",
        "application/wasm",
    }
)


def _is_binary_content_type(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip()
    return media_type.startswith(_BINARY_MAJOR_TYPES) or media_type in _BINARY_MEDIA_TYPES


def scrub_network_body(
    body: str | bytes | None,
    content_type: str = "",
    replacement: str = DEFAULT_REPLACEMENT,
    enabled_patterns: set[str] | None = None,
) -> tuple[str | bytes | None, list[dict[str, Any]]]:
    """
    Scrub PII from a network request/response body.

    A body is text when its content type names text (JSON, form, HTML, XML,
    YAML, ...). When the type is missing or unrecognised (a fetch with a string
    body sends none; a custom vendor type says nothing) the body counts as text
    if it is valid UTF-8, so the type header cannot decide whether a secret is
    stored. A known binary type, or bytes that are not valid UTF-8, come back
    unchanged.
    """
    if body is None:
        return body, []

    ct = content_type.lower()
    if any(marker in ct for marker in _TEXT_TYPE_MARKERS):
        text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    elif _is_binary_content_type(ct):
        return body, []
    elif isinstance(body, bytes):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return body, []
    else:
        text = body

    result = _scrub_typed_text(text, ct, replacement, enabled_patterns)
    if not result.scrubbed:
        return body, []

    scrubbed = result.text.encode("utf-8") if isinstance(body, bytes) else result.text
    return scrubbed, result.hits


def _scrub_typed_text(text: str, content_type: str, replacement: str, enabled_patterns: set[str] | None) -> ScrubResult:
    if "urlencoded" in content_type:
        return _scrub_pairs(text, replacement, enabled_patterns)
    if "json" in content_type or "javascript" in content_type:
        return _scrub_json_text(text, replacement, enabled_patterns)
    return scrub_text(text, replacement, enabled_patterns)


def _secret_names_apply(text: str, enabled_patterns: set[str] | None) -> bool:
    """Whether to look for values that are secrets by name: the pattern that reports them is on, and `text`
    holds a word that could be part of such a name."""
    return (enabled_patterns is None or "password_field" in enabled_patterns) and bool(SECRET_NAME_HINT.search(text))


def _not_covered(found: list[Found], by_name: list[Found]) -> list[Found]:
    """The values found by name that no pattern's match already holds whole, so none is reported twice."""
    covered = merge_spans((start, end) for start, end, _ in found)
    return [item for item in by_name if not inside_any(covered, item[0], item[1])]


def _scrub_pairs(text: str, replacement: str, enabled_patterns: set[str] | None, anchor: str = "") -> ScrubResult:
    """Scrub an urlencoded `key=value&...` string as it arrived, then as its decoded view.

    The raw pass comes first: a pattern can match characters that decoding changes (the `+` in a base64
    token). The decoded view then shows `ops%40example.com` and `4111+1111+1111+1111` as the email and
    the card they are, and each match is redacted in the raw text, so bytes outside a match are never
    rewritten. A parameter whose name says it is a secret loses its whole value, since
    `user[password]=...` matches no pattern, and `api_key_param` and `password_field` also leave the name
    standing (see `_FORM_COMPILED`). `anchor` is what precedes `text` where it came from (`?` for
    a URL query), which `oauth_code` anchors on in the raw pass; the decoded view has no such anchor, so
    a parameter named `co%64e` is not recognised. The cost is two runs of the pattern set over the text,
    whatever the number of pairs.
    """
    plain = _scrub_with(anchor + text, replacement, enabled_patterns, form=True)
    text = plain.text.removeprefix(anchor)
    view = decode_form(text)
    found: list[Found] = []
    if view.changed:
        for start, end, name in find_pii_spans(view.text, enabled_patterns, form=True):
            if name in _FORM_VALUE_ONLY and view.text[start:end] == replacement:
                continue  # the raw pass kept the name and redacted this value
            raw_start, raw_end = view.to_raw(start, end)
            found.append((raw_start, raw_end, _hit_record(name, raw_start, view.text[start:end])))
    if _secret_names_apply(view.text, enabled_patterns):
        found += _not_covered(found, form_secret_values(text, replacement))
    if not found:
        return ScrubResult(text=text, hits=plain.hits)
    spans = merge_spans((start, end) for start, end, _ in found)
    return ScrubResult(
        text=replace_spans(text, spans, urllib.parse.quote_plus(replacement)),
        hits=plain.hits + [hit for _, _, hit in found],
    )


def _scrub_json_text(text: str, replacement: str, enabled_patterns: set[str] | None) -> ScrubResult:
    """Scrub JSON or JavaScript text as it arrived and with its u-escapes written out, and redact them all.

    No single view is enough. Written out, an escaped space or `&` ends the `password` match that the
    escaped text carries through to the end of the value; escaped, a `/` or a space splits a token that
    the written-out text holds whole; and a field name such as `pass\\u0077ord` is only readable once
    its escapes are written out while the value's `\\u0020` has to stay escaped. So the text is matched
    as it arrived, with every escape written out, and with only the token-character escapes written out.
    Every match is redacted in the text as it arrived, so escapes that matched nothing are left as they
    are and the JSON around a match stays JSON. A text with no escape worth writing out is matched as it
    arrived alone, and by the same route: rewriting it first, as `scrub_text` does, lets a match that
    takes a quote or two with it change which quotes open a string, and the member scan then misreads
    the rest of the body. Five patterns redact only their value (see `_JSON_VALUE_ONLY`).
    """
    view = decode_json_escapes(text)
    found: list[Found] = []
    for start, end, name in find_pii_spans(text, enabled_patterns, json=True):
        span = view.snap(start, end)
        found.append((*span, _hit_record(name, span[0], text[start:end], _JSON_VALUE_ONLY)))
    if view.changed:
        token_view = decode_json_token_escapes(text)
        # The token view is the raw text when no token character is escaped, and the full view when only
        # token characters are: either way it holds nothing the other two do not, so it is not matched again.
        for decoded in (view, token_view) if token_view.text not in (text, view.text) else (view,):
            for start, end, name in find_pii_spans(decoded.text, enabled_patterns, json=True):
                span = decoded.to_raw(start, end)
                found.append((*span, _hit_record(name, span[0], decoded.text[start:end], _JSON_VALUE_ONLY)))
    if _secret_names_apply(view.text, enabled_patterns):
        found += _not_covered(found, json_secret_values(text, replacement))
    if not found:
        return ScrubResult(text=text)
    spans, hits = merge_found(found)
    return ScrubResult(text=replace_spans(text, spans, replacement), hits=hits)


def scrub_url_query(
    url: str,
    replacement: str = DEFAULT_REPLACEMENT,
    enabled_patterns: set[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Scrub the keys and values of a URL's query string and of its fragment; the rest of the URL is left as it is.

    An OAuth implicit grant and an OIDC fragment response put `access_token` and `id_token` in the fragment
    as `key=value&...`, so it is read as the query is. (The name is older than the fragment.)
    """
    head, hash_mark, fragment = url.partition("#")
    path, question_mark, query = head.partition("?")
    hits: list[dict[str, Any]] = []
    if query:
        scrubbed_query = _scrub_pairs(query, replacement, enabled_patterns, anchor="?")
        query, hits = scrubbed_query.text, hits + scrubbed_query.hits
    if fragment:
        scrubbed_fragment = _scrub_pairs(fragment, replacement, enabled_patterns, anchor="#")
        fragment, hits = scrubbed_fragment.text, hits + scrubbed_fragment.hits
    if not hits:
        return url, []
    return f"{path}{question_mark}{query}{hash_mark}{fragment}", hits


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
        screenshot_strict: bool = False,
    ):
        self.enabled = enabled
        self.screenshot_enabled = screenshot_enabled and enabled
        self.network_enabled = network_enabled and enabled
        self.console_enabled = console_enabled and enabled
        self.replacement = replacement
        self.enabled_patterns = enabled_patterns  # None = all patterns active
        self.audit_report = audit_report
        # Read by the observation service: withhold a screenshot that cannot be scrubbed.
        self.screenshot_strict = screenshot_strict

    @classmethod
    def from_settings(cls, settings: Any) -> "PiiScrubber":
        """Build from auto-browser Settings object."""
        enabled = getattr(settings, "pii_scrub_enabled", True)
        screenshot = getattr(settings, "pii_scrub_screenshot", True)
        network = getattr(settings, "pii_scrub_network", True)
        console = getattr(settings, "pii_scrub_console", True)
        replacement = getattr(settings, "pii_scrub_replacement", DEFAULT_REPLACEMENT)
        audit = getattr(settings, "pii_scrub_audit_report", True)
        strict = getattr(settings, "pii_scrub_screenshot_strict", False) is True

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
            screenshot_strict=strict,
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

    def url(self, value: str) -> tuple[str, list[dict[str, Any]]]:
        """Scrub the query string of a request URL."""
        if not self.network_enabled:
            return value, []
        return scrub_url_query(value, self.replacement, self.enabled_patterns)

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
