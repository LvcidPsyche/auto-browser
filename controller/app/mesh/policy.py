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
from typing import Optional

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


def _check_url_allowlist(grant: CapabilityGrant, request: DelegationRequest) -> None:
    """
    Enforce url_allowlist constraint.

    If the grant has an allowlist, the request's 'url' argument (if present)
    must match at least one pattern via fnmatch. Empty allowlist = unrestricted.
    """
    if not grant.url_allowlist:
        return  # unrestricted

    url = request.arguments.get("url") or request.arguments.get("start_url") or ""
    if not url:
        return  # no URL argument in request, nothing to check

    for pattern in grant.url_allowlist:
        if fnmatch.fnmatch(url, pattern):
            return  # matched

    raise PolicyDenied(
        f"URL {url!r} not in allowlist for capability {grant.capability!r}. Allowed patterns: {grant.url_allowlist}"
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
