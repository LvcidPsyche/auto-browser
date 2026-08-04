"""
mesh.transport — Envelope signing, verification, and HTTP delivery.

All envelope crypto is real Ed25519.
send_envelope is a real async HTTPS POST — no longer a stub.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from .identity import NodeIdentity
from .models import PeerRecord, SignedEnvelope

logger = logging.getLogger(__name__)

_ENVELOPE_VERSION = "1"
_SEND_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EnvelopeVerificationError(Exception):
    pass


class TransportError(Exception):
    pass


# ---------------------------------------------------------------------------
# Signing helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(envelope: SignedEnvelope) -> bytes:
    """Produce a deterministic byte string over the envelope fields that matter."""
    canonical = {
        "v": _ENVELOPE_VERSION,
        "sender_node_id": envelope.sender_node_id,
        "recipient_node_id": envelope.recipient_node_id,
        "nonce": envelope.nonce,
        "timestamp": envelope.timestamp,
        "payload": envelope.payload,
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()


def make_envelope(identity: NodeIdentity, payload: dict[str, Any], recipient_node_id: str = "") -> SignedEnvelope:
    """
    Build a SignedEnvelope from this node's identity.

    The signature covers: version, sender_node_id, recipient_node_id,
    nonce, timestamp, and payload — in canonical JSON form.
    """
    envelope = SignedEnvelope(
        sender_node_id=identity.node_id,
        recipient_node_id=recipient_node_id,
        payload=payload,
    )
    raw = _canonical_bytes(envelope)
    sig_bytes = identity.sign(raw)
    envelope.signature_b64 = base64.b64encode(sig_bytes).decode()
    return envelope


def verify_envelope(
    envelope: SignedEnvelope,
    expected_peer: PeerRecord,
    *,
    expected_recipient_node_id: str | None = None,
) -> dict[str, Any]:
    """
    Verify a SignedEnvelope against the sender's pinned public key.

    Returns the envelope payload on success.
    Raises EnvelopeVerificationError on any failure.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    # Resolve sender's pinned public key
    if envelope.sender_node_id != expected_peer.node_id:
        raise EnvelopeVerificationError(
            f"sender_node_id {envelope.sender_node_id!r} != expected {expected_peer.node_id!r}"
        )
    if expected_recipient_node_id is not None and envelope.recipient_node_id != expected_recipient_node_id:
        raise EnvelopeVerificationError(
            f"recipient_node_id {envelope.recipient_node_id!r} != expected {expected_recipient_node_id!r}"
        )

    try:
        pub_raw = base64.b64decode(expected_peer.pubkey_b64)
        pub_key = Ed25519PublicKey.from_public_bytes(pub_raw)
    except Exception as exc:
        raise EnvelopeVerificationError(f"Failed to load peer public key: {exc}") from exc

    canonical = _canonical_bytes(envelope)
    try:
        sig_bytes = base64.b64decode(envelope.signature_b64)
        pub_key.verify(sig_bytes, canonical)
    except Exception as exc:
        raise EnvelopeVerificationError(f"Signature verification failed: {exc}") from exc

    return envelope.payload


# ---------------------------------------------------------------------------
# HTTP transport — REAL implementation (was stub)
# ---------------------------------------------------------------------------


async def send_envelope(peer: PeerRecord, envelope: SignedEnvelope) -> SignedEnvelope:
    """
    POST a signed envelope to a peer's /mesh/receive endpoint.

    Returns the peer's reply SignedEnvelope.
    Raises TransportError on connection failure, timeout, or non-2xx response.
    """
    if not peer.endpoint:
        raise TransportError(f"Peer {peer.node_id!r} has no endpoint configured")

    url = peer.endpoint.rstrip("/") + "/mesh/receive"
    payload = envelope.model_dump()

    try:
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_SECONDS, verify=True) as client:
            response = await client.post(url, json=payload)
    except httpx.TimeoutException as exc:
        raise TransportError(f"Timeout sending envelope to {url}: {exc}") from exc
    except httpx.ConnectError as exc:
        raise TransportError(f"Connection error to {url}: {exc}") from exc
    except httpx.RequestError as exc:
        raise TransportError(f"Request error to {url}: {exc}") from exc

    if response.status_code not in (200, 201, 202):
        raise TransportError(
            f"Peer {peer.node_id!r} returned HTTP {response.status_code} from {url}: {response.text[:200]}"
        )

    try:
        data = response.json()
        return SignedEnvelope(**data)
    except Exception as exc:
        raise TransportError(f"Failed to parse reply envelope from {url}: {exc}") from exc
