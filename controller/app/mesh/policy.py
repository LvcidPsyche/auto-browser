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
    try:
        parts = urlsplit(browser_equivalent_url(url))
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if not host or parts.username is not None or parts.password is not None:
        return False

    pattern = pattern.strip()
    if "://" not in pattern:
        return fnmatch.fnmatchcase(host, pattern.lower())

    pattern_scheme, _, pattern_rest = pattern.partition("://")
    pattern_authority, slash, pattern_path = pattern_rest.partition("/")
    authority = f"[{host}]" if ":" in host else host  # IPv6 literals keep their brackets
    if port is not None:
        authority = f"{authority}:{port}"
    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"
    if parts.fragment:
        path += f"#{parts.fragment}"
    return (
        fnmatch.fnmatchcase(parts.scheme.lower(), pattern_scheme.lower())
        # Brackets in an authority are IPv6 delimiters, not fnmatch classes.
        and fnmatch.fnmatchcase(authority, pattern_authority.lower().translate(_LITERAL_BRACKETS))
        and fnmatch.fnmatchcase(path, f"/{pattern_path}" if slash else "/")
    )


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
                f"URL {url!r} not in allowlist for capability {grant.capability!r}. "
                f"Allowed patterns: {grant.url_allowlist}"
            )


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

        logger.info(
            "mesh.policy: PERMIT peer=%s capability=%s require_approval=%s",
            peer.node_id,
            request.capability,
            matching_grant.require_approval,
        )
        return matching_grant

    @staticmethod
    def _find_grant(peer: PeerRecord, capability: str) -> Optional[CapabilityGrant]:
        """
        Find the first grant that matches the requested capability.

        Supports exact matches and wildcard suffix:
          "tool:browser.click"   matches "tool:browser.click"
          "tool:*"               matches any tool capability
          "session:*"            matches any session capability
        """
        for grant in peer.grants:
            pattern = grant.capability
            if pattern == capability:
                return grant
            if pattern.endswith(":*"):
                prefix = pattern[:-1]  # "tool:"
                if capability.startswith(prefix):
                    return grant
        return None
