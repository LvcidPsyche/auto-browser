"""
mesh.delegation — Orchestrator for outbound and inbound capability delegation.

receive_inbound is fully implemented: routes tool/session capabilities,
honors require_approval, returns DelegationResponse.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import Counter, OrderedDict
from threading import Lock
from typing import Any, Awaitable, Callable, Optional

from .identity import NodeIdentity
from .models import (
    DelegationRequest,
    DelegationResponse,
    SignedEnvelope,
)
from .peers import PeerRegistryFile
from .policy import PolicyError, PolicyEvaluator
from .transport import (
    EnvelopeVerificationError,
    TransportError,
    make_envelope,
    send_envelope,
    verify_envelope,
)

logger = logging.getLogger(__name__)

# Default timestamp window: reject envelopes older than this many seconds
_DEFAULT_TIMESTAMP_WINDOW = 30.0
# Nonce cache max size (LRU)
_NONCE_CACHE_MAX = 10_000
# How many of them one sender's unexpired nonces may hold, so one peer's flood refuses only that peer (R5C-17).
_NONCE_CACHE_MAX_PER_SENDER = _NONCE_CACHE_MAX // 10


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DelegationError(Exception):
    pass


class DelegationRejected(DelegationError):
    pass


class DelegationReplayError(DelegationError):
    pass


class MeshReplayCacheFull(DelegationError):
    """The nonce cache, or its sender's share of it, holds its cap of nonces still inside their window: the
    envelope is refused, not recorded."""


# ---------------------------------------------------------------------------
# Nonce cache
# ---------------------------------------------------------------------------


class _NonceCache:
    """Thread-safe nonce cache for replay defense: bounded by age (when `ttl_seconds` is set) and by size.

    A nonce only needs remembering while its envelope could still pass the timestamp check: the manager records
    each nonce with its envelope's timestamp and sets the age to the timestamp window, so a nonce goes once the
    highest wall time seen so far is more than a window past that timestamp. That high-water mark never moves
    back, so a wall clock stepped back cannot free a nonce whose envelope is inside the window again (a monotonic
    age could).

    The size cap bounds how many nonces are remembered at once. With a TTL (the manager's cache always has one),
    nothing remembered is dropped before it expires: at the cap every expired entry goes (a full scan, since entries
    are in insertion order, not stamp order), and if none had expired the new nonce is refused with
    MeshReplayCacheFull and not recorded. A cache without a TTL, which the manager never builds, still evicts its
    oldest entry at the cap.

    `max_per_owner` bounds the nonces one owner (the sender the manager names) holds at once, the same way: an owner
    at its share is refused with MeshReplayCacheFull once none of its nonces has expired, and the others are not.
    """

    def __init__(
        self,
        max_size: int = _NONCE_CACHE_MAX,
        ttl_seconds: Optional[float] = None,
        clock: Callable[[], float] = time.time,
        max_per_owner: Optional[int] = None,
    ) -> None:
        self._max = max_size
        self._ttl = ttl_seconds
        self._clock = clock
        self._max_per_owner = max_per_owner
        # Each nonce's stamp and owner, in insertion order, and how many nonces each owner holds.
        self._seen: OrderedDict[str, tuple[float, Optional[str]]] = OrderedDict()
        self._held: Counter[Optional[str]] = Counter()
        self._high_water = float("-inf")
        self._lock = Lock()

    @property
    def high_water(self) -> float:
        """The highest wall time a check has seen (-inf before the first): the clock the nonces expire by."""
        with self._lock:
            return self._high_water

    def check_and_record(
        self,
        nonce: str,
        *,
        stamped_at: Optional[float] = None,
        now: Optional[float] = None,
        owner: Optional[str] = None,
    ) -> bool:
        """
        Returns True (first time seen → OK) or False (replay detected → reject); raises MeshReplayCacheFull when a
        cache with a TTL is full of unexpired nonces, or `owner` holds its share of them.

        `stamped_at` is the envelope's timestamp (the time of the check when absent); `now` the wall time the
        caller checked it against (the cache's clock when absent); `owner` the sender the nonce is counted against.
        """
        with self._lock:
            current = self._clock() if now is None else now
            self._high_water = max(self._high_water, current)
            if self._ttl is not None:
                # Roughly oldest first (insertion order); a later entry with an older stamp only stays longer.
                while self._seen and self._high_water - next(iter(self._seen.values()))[0] > self._ttl:
                    self._forget(next(iter(self._seen)))
            if nonce in self._seen:
                return False
            if self._max_per_owner is not None and self._held[owner] >= self._max_per_owner:
                self._forget_expired()
                if self._held[owner] >= self._max_per_owner:
                    raise MeshReplayCacheFull("mesh replay cache holds this sender's share of nonces; retry later")
            if len(self._seen) >= self._max:
                if self._ttl is None:
                    self._forget(next(iter(self._seen)))
                else:
                    self._forget_expired()
                    if len(self._seen) >= self._max:
                        raise MeshReplayCacheFull("mesh replay cache is full; retry later")
            self._seen[nonce] = (current if stamped_at is None else stamped_at, owner)
            self._held[owner] += 1
            return True

    def _forget(self, nonce: str) -> None:
        _stamp, owner = self._seen.pop(nonce)
        self._held[owner] -= 1
        if not self._held[owner]:
            del self._held[owner]

    def _forget_expired(self) -> None:
        """Drop every expired nonce (a full scan: entries are in insertion order, not stamp order); none without a TTL."""
        if self._ttl is None:
            return
        for expired in [seen for seen, (stamp, _owner) in self._seen.items() if self._high_water - stamp > self._ttl]:
            self._forget(expired)


# ---------------------------------------------------------------------------
# Tool gateway type
# ---------------------------------------------------------------------------

# The tool gateway is a callable that takes (tool_name, arguments, session_id)
# and returns a result dict.  Wired in at startup by the controller.
ToolGatewayFn = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Approval queue type
# ---------------------------------------------------------------------------

# Enqueue an approval and wait for it.  Returns the approval result dict.
ApprovalFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


class DelegationManager:
    """
    Manages outbound delegation (this node asking peers to do things)
    and inbound delegation (peers asking this node to do things).
    """

    def __init__(
        self,
        identity: NodeIdentity,
        peers: PeerRegistryFile,
        timestamp_window: float = _DEFAULT_TIMESTAMP_WINDOW,
        tool_gateway: Optional[ToolGatewayFn] = None,
        approval_fn: Optional[ApprovalFn] = None,
    ) -> None:
        self._identity = identity
        self._peers = peers
        self._timestamp_window = timestamp_window
        self._policy = PolicyEvaluator()
        # A nonce is kept until the wall clock's high-water mark is a window past its envelope's timestamp, after
        # which that envelope can no longer pass the timestamp check; each sender's against its own share.
        self._nonce_cache = _NonceCache(ttl_seconds=timestamp_window, max_per_owner=_NONCE_CACHE_MAX_PER_SENDER)
        # Nonces are not persisted, so an envelope the last process could have accepted is refused by its timestamp
        # instead: that process accepted envelopes stamped up to one window ahead of its clock, so up to one window
        # past this start. The cost is about one window of refusals after each start, which a sender's retry passes.
        self._started_at = time.time()
        self._tool_gateway = tool_gateway
        self._approval_fn = approval_fn

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def delegate(self, peer_node_id: str, request: DelegationRequest) -> DelegationResponse:
        """
        Ask a peer to execute a capability on our behalf.

        Signs the request as a DelegationRequest payload, POSTs it to the peer's
        /mesh/receive endpoint, verifies the reply envelope, and returns the
        decoded DelegationResponse.
        """
        peer = self._peers.get(peer_node_id)
        payload = request.model_dump(mode="json")
        envelope = make_envelope(
            identity=self._identity,
            payload=payload,
            recipient_node_id=peer_node_id,
        )

        try:
            reply_envelope = await send_envelope(peer=peer, envelope=envelope)
        except TransportError:
            raise

        try:
            reply_payload = verify_envelope(
                reply_envelope,
                expected_peer=peer,
                expected_recipient_node_id=self._identity.node_id,
            )
        except EnvelopeVerificationError as exc:
            logger.warning(
                "mesh.delegation.reply_verification_failed peer=%s error=%s",
                peer_node_id,
                exc,
            )
            raise DelegationError(f"reply envelope verification failed: {exc}") from exc

        # Reply timestamp window — symmetric with inbound, closes a MITM replay vector
        # where a captured reply could be re-served long after the original. Against the clock the nonces expire by,
        # as the inbound check is (W2-2): read raw, a clock stepped back put a reply whose nonce had expired inside
        # the window again, and it was accepted twice.
        reply_now = max(time.time(), self._nonce_cache.high_water)
        reply_age = reply_now - reply_envelope.timestamp
        # "not <=", not ">": a NaN timestamp compares false both ways and would pass ">".
        if not abs(reply_age) <= self._timestamp_window:
            raise DelegationError(
                f"reply timestamp out of window: age={reply_age:.1f}s "
                f"limit={self._timestamp_window}s peer={peer_node_id}"
            )

        if not self._nonce_cache.check_and_record(
            reply_envelope.nonce, stamped_at=reply_envelope.timestamp, now=reply_now, owner=peer_node_id
        ):
            raise DelegationReplayError(f"reply nonce replay from peer={peer_node_id}")

        response = DelegationResponse.model_validate(reply_payload)
        # A reply to another request (a stale one replayed, W2-4) is not this request's answer.
        if response.request_id != request.request_id:
            raise DelegationRejected(f"reply request id does not match the request's from peer={peer_node_id}")
        return response

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def receive_inbound(self, envelope: SignedEnvelope) -> DelegationResponse:
        """
        Handle an incoming signed envelope from a peer.

        Steps:
        1. Resolve sender peer from registry (deny unknown peers).
        2. Verify envelope signature + pubkey pinning.
        3. Enforce timestamp window and nonce freshness.
        4. Decode DelegationRequest from payload.
        5. Evaluate policy — deny or continue.
        6. If require_approval: gate on approval queue.
        7. Route capability to tool gateway / session handler.
        8. Return DelegationResponse.
        """
        sender_id = envelope.sender_node_id
        peer = self._peers.get_optional(sender_id)
        if peer is None:
            raise DelegationRejected(f"Unknown sender node_id={sender_id!r}")

        # Timestamp window — check before signature verification (cheap, fails fast). Against the clock the nonces
        # expire by, which never moves back (CB3-1): read raw, a clock stepped back put an envelope whose nonce had
        # already expired inside the window again, and it ran twice. The cost: after a large backward step, fresh
        # envelopes are refused until the wall clock catches up (about the step minus the window).
        now = max(time.time(), self._nonce_cache.high_water)
        age = now - envelope.timestamp
        # "not <=", not ">": a NaN timestamp compares false both ways and would pass ">".
        if not abs(age) <= self._timestamp_window:
            raise DelegationRejected(
                f"envelope timestamp out of window: age={age:.1f}s limit={self._timestamp_window}s"
            )

        if envelope.timestamp <= self._started_at + self._timestamp_window:
            raise DelegationRejected(
                "envelope was stamped before this node started, or within one timestamp window after it; the nonce "
                "cache does not survive a restart, so retry"
            )

        # Signature verification BEFORE nonce cache write.
        # Rationale: if we record the nonce first, any unauthenticated attacker
        # can flood the LRU cache (10k entries) and evict legitimate nonces.
        # Only record nonces for envelopes we have authenticated.
        try:
            payload = verify_envelope(
                envelope,
                expected_peer=peer,
                expected_recipient_node_id=self._identity.node_id,
            )
        except EnvelopeVerificationError as exc:
            logger.warning("mesh.delegation.envelope_invalid sender=%s error=%s", sender_id, exc)
            raise DelegationRejected(f"envelope verification failed: {exc}") from exc

        # Decode request
        try:
            request = DelegationRequest.model_validate(payload)
        except Exception as exc:
            raise DelegationRejected(f"Malformed DelegationRequest: {exc}") from exc

        # Nonce freshness — now safe to record: the sender is authenticated, and its request decodes (one that does
        # not runs nothing, and neither does its replay). Counted against the sender's own share of the cache, so a
        # peer that floods it refuses only itself (R5C-17); a replay of a request that runs still meets its nonce.
        try:
            fresh = self._nonce_cache.check_and_record(
                envelope.nonce, stamped_at=envelope.timestamp, now=now, owner=sender_id
            )
        except MeshReplayCacheFull as exc:
            logger.warning("mesh.delegation.replay_cache_full sender=%s: the envelope was refused: %s", sender_id, exc)
            raise
        if not fresh:
            raise DelegationReplayError(f"nonce replay from sender={sender_id!r}")

        # Policy evaluation
        try:
            grant = self._policy.evaluate(peer, request)
        except PolicyError as exc:
            logger.info("mesh.policy.denied peer=%s capability=%s reason=%s", sender_id, request.capability, exc)
            return DelegationResponse(
                request_id=request.request_id,
                status="rejected",
                error="Delegation policy rejected the request",
            )

        # Update peer last_seen
        self._peers.update_last_seen(sender_id)

        # Approval gate
        if grant.require_approval:
            if self._approval_fn is None:
                return DelegationResponse(
                    request_id=request.request_id,
                    status="approval_required",
                    error="Approval required but no approval handler configured",
                )
            approval_id = str(uuid.uuid4())
            logger.info(
                "mesh.delegation.approval_required peer=%s capability=%s approval_id=%s",
                sender_id,
                request.capability,
                approval_id,
            )
            try:
                approval_result = await self._approval_fn(approval_id, request.model_dump())
            except Exception:
                return DelegationResponse(
                    request_id=request.request_id,
                    status="approval_required",
                    approval_id=approval_id,
                    error="Delegation approval failed",
                )
            # Inspect approval outcome: any status != "approved" blocks routing.
            # Convention: approval_fn returns {"status": "approved"|"denied"|...} dict.
            approval_status = (approval_result or {}).get("status", "denied")
            if approval_status in {"pending", "approval_required"}:
                return DelegationResponse(
                    request_id=request.request_id,
                    status="approval_required",
                    approval_id=approval_id,
                    error=f"Approval pending (status={approval_status})",
                )
            if approval_status != "approved":
                logger.info(
                    "mesh.delegation.approval_denied peer=%s capability=%s approval_id=%s status=%s",
                    sender_id,
                    request.capability,
                    approval_id,
                    approval_status,
                )
                return DelegationResponse(
                    request_id=request.request_id,
                    status="rejected",
                    approval_id=approval_id,
                    error=f"Approval denied (status={approval_status})",
                )

        # Route capability
        result = await self._route_capability(request)
        if result.get("status") == "approval_required":
            return DelegationResponse(
                request_id=request.request_id,
                status="approval_required",
                approval_id=result.get("approval_id"),
                error=result.get("error"),
                result={k: v for k, v in result.items() if k != "_mesh_error"},
            )
        if result.get("_mesh_error"):
            return DelegationResponse(
                request_id=request.request_id,
                status="rejected",
                error=str(result.get("error") or "Delegated capability failed"),
                result={k: v for k, v in result.items() if k != "_mesh_error"},
            )
        return DelegationResponse(
            request_id=request.request_id,
            status="ok",
            result=result,
        )

    async def _route_capability(self, request: DelegationRequest) -> dict[str, Any]:
        """Route a capability string to the appropriate handler."""
        cap = request.capability

        # tool:<name> → tool gateway
        if cap.startswith("tool:"):
            tool_name = cap[5:]  # strip "tool:"
            if self._tool_gateway is None:
                return {"_mesh_error": True, "error": "No tool gateway configured", "tool": tool_name}
            result = await self._tool_gateway(
                tool_name,
                request.arguments,
                request.session_id,
            )
            return result

        # session:<verb> → session handler stub
        if cap.startswith("session:"):
            verb = cap[8:]
            logger.info("mesh.delegation.session_verb verb=%s session_id=%s", verb, request.session_id)
            return {"_mesh_error": True, "error": "session delegation not yet wired", "verb": verb}

        return {"_mesh_error": True, "error": f"Unknown capability kind: {cap!r}"}
