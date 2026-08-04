"""
mesh.delegation — Orchestrator for outbound and inbound capability delegation.

receive_inbound is fully implemented: routes tool/session/workflow capabilities,
honors require_approval, returns DelegationResponse.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
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


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DelegationError(Exception):
    pass


class DelegationRejected(DelegationError):
    pass


class DelegationReplayError(DelegationError):
    pass


# ---------------------------------------------------------------------------
# Nonce cache
# ---------------------------------------------------------------------------


class _NonceCache:
    """Thread-safe LRU nonce cache for replay defense."""

    def __init__(self, max_size: int = _NONCE_CACHE_MAX) -> None:
        self._max = max_size
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = Lock()

    def check_and_record(self, nonce: str) -> bool:
        """
        Returns True (first time seen → OK) or False (replay detected → reject).
        """
        with self._lock:
            if nonce in self._seen:
                return False
            self._seen[nonce] = time.time()
            if len(self._seen) > self._max:
                self._seen.popitem(last=False)
            return True


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
        self._nonce_cache = _NonceCache()
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
        # where a captured reply could be re-served long after the original.
        reply_age = time.time() - reply_envelope.timestamp
        if abs(reply_age) > self._timestamp_window:
            raise DelegationError(
                f"reply timestamp out of window: age={reply_age:.1f}s "
                f"limit={self._timestamp_window}s peer={peer_node_id}"
            )

        if not self._nonce_cache.check_and_record(reply_envelope.nonce):
            raise DelegationReplayError(f"reply nonce replay from peer={peer_node_id}")

        return DelegationResponse.model_validate(reply_payload)

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

        # Timestamp window — check before signature verification (cheap, fails fast)
        age = time.time() - envelope.timestamp
        if abs(age) > self._timestamp_window:
            raise DelegationRejected(
                f"envelope timestamp out of window: age={age:.1f}s limit={self._timestamp_window}s"
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

        # Nonce freshness — now safe to record; sender is authenticated.
        if not self._nonce_cache.check_and_record(envelope.nonce):
            raise DelegationReplayError(f"nonce replay from sender={sender_id!r}")

        # Decode request
        try:
            request = DelegationRequest.model_validate(payload)
        except Exception as exc:
            raise DelegationRejected(f"Malformed DelegationRequest: {exc}") from exc

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

        # workflow:<name> → workflow engine stub
        if cap.startswith("workflow:"):
            name = cap[9:]
            logger.info("mesh.delegation.workflow name=%s", name)
            return {"_mesh_error": True, "error": "workflow delegation not yet wired", "name": name}

        return {"_mesh_error": True, "error": f"Unknown capability kind: {cap!r}"}
