"""
mesh.policy — Capability grant evaluator.

Default-deny: if no matching grant exists, the request is rejected.
All four constraint evaluators are real implementations (no stubs).
"""

from __future__ import annotations

import fnmatch
import logging
import time
from collections import defaultdict
from threading import Lock
from typing import Any, Optional
from urllib.parse import urlsplit

from ..url_safety import browser_equivalent_url
from ..utils import capped_for_log
from .models import CapabilityGrant, DelegationRequest, PeerRecord

logger = logging.getLogger(__name__)


class PolicyError(Exception):
    pass


class PolicyDenied(PolicyError):
    pass


class PolicyRateLimited(PolicyError):
    pass


class PolicyExpired(PolicyError):
    pass


# ---------------------------------------------------------------------------
# Per-process invocation counter (resets on restart — fine for PoC rate limit)
# ---------------------------------------------------------------------------

_invocation_lock = Lock()
_invocation_counts: dict[str, list[float]] = defaultdict(list)  # key → [timestamp, ...]


def _record_invocation(key: str) -> None:
    now = time.time()
    with _invocation_lock:
        _invocation_counts[key].append(now)
        # prune entries older than 1 hour
        cutoff = now - 3600
        _invocation_counts[key] = [t for t in _invocation_counts[key] if t > cutoff]


def _count_invocations(key: str) -> int:
    now = time.time()
    cutoff = now - 3600
    with _invocation_lock:
        _invocation_counts[key] = [t for t in _invocation_counts[key] if t > cutoff]
        return len(_invocation_counts[key])


# ---------------------------------------------------------------------------
# Constraint evaluators
# ---------------------------------------------------------------------------


# Argument keys whose values are URLs the delegated tool will load. Collected at
# any depth: browser.execute_action carries its navigation target at
# action.url, and set_cookies at cookies[].url.
_URL_ARGUMENT_KEYS = frozenset({"url", "start_url", "urls", "cdp_url"})
_LITERAL_BRACKETS = str.maketrans({"[": "[[]", "]": "[]]"})


def _url_arguments(arguments: Any) -> list[str]:
    found: list[str] = []

    def walk(node: Any, key: str | None) -> None:
        if isinstance(node, dict):
            for child_key, child in node.items():
                walk(child, str(child_key))
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, key)
        elif key in _URL_ARGUMENT_KEYS and node is not None:
            found.append(str(node))

    walk(arguments, None)
    return found


def _url_matches(url: str, pattern: str) -> bool:
    """Match one URL against one allowlist pattern, component by component.

    Patterns were fnmatch'd against the whole URL, where `*` also matches "/",
    "?", "#" and "@": `https://*.example.com/*` admitted
    `https://evil.com/.example.com/`, and `*example.com*` admitted
    `https://evil.com/?example.com`. Now a pattern with a scheme
    (`https://*.example.com/*`) matches scheme, authority and path separately,
    so no wildcard reaches across them; a pattern without one
    (`*.example.com`) matches the host alone. A URL carrying userinfo never
    matches — `https://example.com@evil.com/` is how a host is disguised.
    """
    read = _url_parts(url)
    if read is None:
        return False

    pattern = pattern.strip()
    if "://" not in pattern:
        return fnmatch.fnmatchcase(read[1], pattern.lower())

    allows = _pattern_allows(read, pattern)
    return allows["scheme"] and allows["authority"] and allows["path"]


def _url_parts(url: str) -> tuple[str, str, str, str] | None:
    """A URL as the allowlist reads it: scheme, host, authority (the host with its port as written; an IPv6 literal
    keeps its brackets) and path (with its query and fragment). None for one no pattern matches: no host,
    userinfo, or a port that cannot be read."""
    try:
        parts = urlsplit(browser_equivalent_url(url))
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host or parts.username is not None or parts.password is not None:
        return None
    authority = f"[{host}]" if ":" in host else host  # IPv6 literals keep their brackets
    if port is not None:
        authority = f"{authority}:{port}"
    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"
    if parts.fragment:
        path += f"#{parts.fragment}"
    return parts.scheme.lower(), host, authority, path


def _pattern_allows(read: tuple[str, str, str, str], pattern: str) -> dict[str, bool]:
    """Which parts of a URL (_url_parts) a pattern with a scheme allows: scheme, authority and path, each matched
    alone (_url_matches), and the host with any port, for what a refusal says of the URL (_refused_url_shown)."""
    scheme, host, authority, path = read
    pattern_scheme, _, pattern_rest = pattern.partition("://")
    pattern_authority, slash, pattern_path = pattern_rest.partition("/")
    pattern_authority = pattern_authority.lower()
    # The pattern's host: its authority without a port after it.
    bracket = pattern_authority.find("]") if pattern_authority.startswith("[") else -1
    pattern_host = pattern_authority[: bracket + 1] if bracket >= 0 else pattern_authority.rpartition(":")[0]
    # Brackets in an authority are IPv6 delimiters, not fnmatch classes.
    authority_allowed = fnmatch.fnmatchcase(authority, pattern_authority.translate(_LITERAL_BRACKETS))
    host_allowed = fnmatch.fnmatchcase(
        f"[{host}]" if ":" in host else host, (pattern_host or pattern_authority).translate(_LITERAL_BRACKETS)
    )
    return {
        "scheme": fnmatch.fnmatchcase(scheme, pattern_scheme.lower()),
        "authority": authority_allowed,
        "host": authority_allowed or host_allowed,
        "path": fnmatch.fnmatchcase(path, f"/{pattern_path}" if slash else "/"),
    }


def _check_url_allowlist(grant: CapabilityGrant, request: DelegationRequest) -> None:
    """
    Enforce url_allowlist constraint.

    If the grant has an allowlist, every URL the request carries — `url`,
    `start_url`, `urls` or `cdp_url`, at any depth — must match at least one
    pattern (see _url_matches). Empty allowlist = unrestricted.
    """
    if not grant.url_allowlist:
        return  # unrestricted

    for url in _url_arguments(request.arguments):
        if not any(_url_matches(url, pattern) for pattern in grant.url_allowlist):
            raise PolicyDenied(
                f"{_refused_url_shown(url, grant.url_allowlist)} is not in the allowlist for capability "
                f"{grant.capability!r}. Allowed patterns: {grant.url_allowlist}"
            )


# The parts of a URL a refusal names (_refused_url_shown), by the key _pattern_allows gives each.
_PARTS_NAMED = (("scheme", "scheme"), ("port", "authority"), ("path", "path"))


def _refused_url_shown(url: str, patterns: list[str]) -> str:
    """What a refusal says of the URL it refuses: its host name, and what no pattern allows in it. Never the URL.

    The reason is logged (mesh.policy.denied), and the URL is the peer's text as it sent it: the userinfo, the path
    and the query in it are not for a log. A URL with userinfo is refused for having it (_url_matches), and so is
    one whose port cannot be read (99999, abc), so the reason says so: its host alone may be one a pattern allows.
    So may the host of a URL whose scheme, port or path no pattern of that host allows: the reason names that part,
    or says that each such pattern refuses another. The host is the peer's text too, as long as a request may be:
    it is quoted, and cut as a log line's text is.
    """
    try:
        parts = urlsplit(browser_equivalent_url(url))
    except ValueError:
        return "A URL that cannot be read"
    if not parts.hostname:
        return "A URL with no host name"
    shown = f"A URL of host {capped_for_log(parts.hostname)!r}"
    if parts.username is not None or parts.password is not None:
        return f"{shown} that carries userinfo, which no pattern allows,"
    try:
        parts.port  # read for the ValueError it raises on such a port, where _url_matches refuses the URL
    except ValueError:
        return f"{shown} and a port that cannot be read, which no pattern allows,"
    read = _url_parts(url)
    if read is None:
        return shown
    # What each pattern that allows the host refuses. A pattern with no scheme matches the host alone: one that
    # allows it would have allowed the URL.
    refused_by_each = [
        [name for name, key in _PARTS_NAMED if not allows[key]]
        for allows in (_pattern_allows(read, pattern.strip()) for pattern in patterns if "://" in pattern)
        if allows["host"]
    ]
    if not refused_by_each or [] in refused_by_each:  # no pattern allows the host; one allows the URL
        return shown
    named = [name for name, _ in _PARTS_NAMED if all(name in refused for refused in refused_by_each)]
    if not named:
        return f"{shown} with a scheme, port and path no one pattern allows together,"
    # The port of a URL that writes none is refused only by patterns that each name one ("a port no pattern allows"
    # named a port the URL does not write, and with :443 the one such a pattern allows).
    no_port = ""
    if "port" in named and parts.port is None:
        named.remove("port")
        no_port = "and no port written, where each pattern of that host names one,"
        if not named:
            return f"{shown} {no_port}"
    if "path" in named and ("?" in read[3] or "#" in read[3]):
        named[-1] = "path (with its query or fragment)"
    listed = named[0] if len(named) == 1 else f"{', '.join(named[:-1])} and {named[-1]}"
    return f"{shown} and a {listed} no pattern allows with that host,{' ' + no_port if no_port else ''}"


def _check_expires_at(grant: CapabilityGrant) -> None:
    """Reject if the grant has expired."""
    if grant.expires_at == 0.0:
        return  # never expires
    if time.time() > grant.expires_at:
        raise PolicyExpired(
            f"Grant for capability {grant.capability!r} expired at {grant.expires_at} (now={time.time():.1f})"
        )


def _check_rate_limit(grant: CapabilityGrant, peer_node_id: str) -> None:
    """Enforce max_invocations_per_hour rolling window."""
    if grant.max_invocations_per_hour == 0:
        return  # unlimited
    key = f"{peer_node_id}:{grant.capability}"
    current = _count_invocations(key)
    if current >= grant.max_invocations_per_hour:
        raise PolicyRateLimited(
            f"Rate limit exceeded for capability {grant.capability!r} "
            f"by peer {peer_node_id!r}: {current}/{grant.max_invocations_per_hour} per hour"
        )


def _record_invocation_for_grant(grant: CapabilityGrant, peer_node_id: str) -> None:
    """Call after policy passes — records the invocation for rate limiting."""
    if grant.max_invocations_per_hour > 0:
        key = f"{peer_node_id}:{grant.capability}"
        _record_invocation(key)


# ---------------------------------------------------------------------------
# Main evaluator
# ---------------------------------------------------------------------------


class PolicyEvaluator:
    """
    Evaluates whether a peer's DelegationRequest is permitted.

    Usage::

        evaluator = PolicyEvaluator()
        grant = evaluator.evaluate(peer, request)  # raises PolicyDenied/etc. on failure
        # grant.require_approval tells receive_inbound whether to gate on approval
    """

    def evaluate(self, peer: PeerRecord, request: DelegationRequest) -> CapabilityGrant:
        """
        Find a matching grant and run all constraint evaluators.

        Returns the matching CapabilityGrant (so callers can read require_approval).
        Raises PolicyDenied, PolicyRateLimited, or PolicyExpired on failure.
        """
        matching_grant = self._find_grant(peer, request.capability)

        if matching_grant is None:
            raise PolicyDenied(
                f"No grant for capability {request.capability!r} from peer {peer.node_id!r}. Default-deny."
            )

        # Run all constraint evaluators
        _check_expires_at(matching_grant)
        _check_rate_limit(matching_grant, peer.node_id)
        _check_url_allowlist(matching_grant, request)

        # Record the invocation for rate limiting (after all checks pass)
        _record_invocation_for_grant(matching_grant, peer.node_id)

        # %r: under a wildcard grant the capability is the peer's own text past the prefix, and a line break in it
        # must not start a log line.
        logger.info(
            "mesh.policy: PERMIT peer=%s capability=%r require_approval=%s",
            peer.node_id,
            request.capability,
            matching_grant.require_approval,
        )
        return matching_grant

    @staticmethod
    def _find_grant(peer: PeerRecord, capability: str) -> Optional[CapabilityGrant]:
        """
        Find the most specific grant that matches the requested capability.

        Supports exact matches and wildcard suffix:
          "tool:browser.observe" matches "tool:browser.observe"
          "tool:*"               matches any tool capability
          "session:*"            matches any session capability

        An exact match beats a wildcard, a longer wildcard prefix beats a shorter one, and equal
        specificity goes to the grant listed first, so a broad grant listed early cannot shadow a
        stricter one for the same capability.
        """
        best: Optional[CapabilityGrant] = None
        best_rank = (-1, -1)
        for grant in peer.grants:
            pattern = grant.capability
            if pattern == capability:
                rank = (1, len(pattern))
            elif pattern.endswith(":*") and capability.startswith(pattern[:-1]):  # "tool:*" -> "tool:"
                rank = (0, len(pattern) - 1)
            else:
                continue
            if rank > best_rank:
                best, best_rank = grant, rank
        return best
