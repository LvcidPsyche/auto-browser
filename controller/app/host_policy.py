"""Small exact-host navigation policy shared by controller actions."""

from __future__ import annotations

from collections.abc import Iterable


def host_is_allowed(host: str, patterns: Iterable[str]) -> bool:
    normalized = [pattern.lower() for pattern in patterns]
    return not normalized or "*" in normalized or host.lower() in normalized
