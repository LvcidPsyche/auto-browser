from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> str:
    """Return current UTC timestamp as ISO-8601 string with Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
