"""Canonical allow-list validation shared by tenant administration paths."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Iterable

MAX_ALLOWED_HOSTS = 64
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_hostname(value: str) -> str:
    """Return one canonical DNS A-label, rejecting origin-like input and IPs."""

    if not isinstance(value, str):
        raise ValueError("hostname must be a string")
    hostname = value.strip()
    if not hostname or any(character in hostname for character in ":/@?#*"):
        raise ValueError("hostname must be a bare DNS hostname")
    if hostname.endswith(".") or ".." in hostname:
        raise ValueError("hostname must be a canonical DNS hostname")
    try:
        alabel = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("hostname is not valid IDNA") from exc
    if len(alabel) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in alabel.split(".")):
        raise ValueError("hostname must be a valid DNS hostname")
    try:
        ipaddress.ip_address(alabel)
    except ValueError:
        try:
            socket.inet_aton(alabel)
        except OSError:
            pass
        else:
            raise ValueError("IP literals are not allowed hostnames") from None
    else:
        raise ValueError("IP literals are not allowed hostnames")
    return alabel


def normalize_hostnames(
    values: str | Iterable[str], *, max_hosts: int = MAX_ALLOWED_HOSTS,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    """Canonicalize, deduplicate, and bound an allow-list of DNS hostnames."""

    if max_hosts < 1:
        raise ValueError("max_hosts must be positive")
    source = values.split(",") if isinstance(values, str) else values
    normalized: list[str] = []
    seen: set[str] = set()
    for value in source:
        hostname = normalize_hostname(value)
        if hostname not in seen:
            normalized.append(hostname)
            seen.add(hostname)
    if not normalized and not allow_empty:
        raise ValueError("at least one hostname is required")
    if len(normalized) > max_hosts:
        raise ValueError(f"at most {max_hosts} hostnames are allowed")
    return tuple(normalized)
