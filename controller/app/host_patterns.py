"""What a CONTROLLER_ALLOWED_HOSTS entry is, as the Host check reads it (app/middleware/hosts.py).

Here, apart from the check, so that the runtime policy reads the same grammar without importing the middleware
package, whose ``__init__`` loads ``http.py`` and FastAPI's stack: ``python -m app.api_token``, which the image's
entrypoint runs, started about 0.8 s slower for it. This imports nothing of the app's middleware or of FastAPI.
"""

from __future__ import annotations

import ipaddress


def normalize_pattern(pattern: str) -> str:
    normalized = pattern.strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return normalized


def is_wildcard_pattern(pattern: str) -> bool:
    """Whether a CONTROLLER_ALLOWED_HOSTS entry is a wildcard, read as the Host check reads it.

    On the check's own normalization, so no spelling escapes it: case and brackets change nothing the match reads,
    and a malformed wildcard is refused when the app is built (pattern_error), not read as an exact name.
    """
    return "*" in normalize_pattern(pattern)


def pattern_error(pattern: str) -> str | None:
    """Why the Host check refuses ``pattern`` when the app is built, or None for a pattern it takes."""
    stripped = pattern.strip()
    if "[" in stripped or "]" in stripped:
        # Brackets are how an IPv6 address is written in a Host. Round anything else they were stripped all the same,
        # and "[*]" admitted every Host as "*" does.
        if not (stripped.startswith("[") and stripped.endswith("]") and _is_ipv6_address(stripped[1:-1])):
            return "brackets may only hold an IPv6 address"
        return None
    normalized = normalize_pattern(pattern)
    if "*" not in normalized or normalized == "*":
        return None
    # "*." matched every name written with its trailing dot.
    if normalized.startswith("*.") and len(normalized) > 2 and "*" not in normalized[1:]:
        return None
    return "wildcards must look like '*.example.com'"


def _is_ipv6_address(text: str) -> bool:
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True
