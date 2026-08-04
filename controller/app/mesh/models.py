"""
mesh.models — Wire formats for the auto-browser peer mesh.

All models are credential-free by design: no passwords, no API keys.
Authentication is done entirely through Ed25519 envelope signatures.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Capability types
# ---------------------------------------------------------------------------


class CapabilityKind(str, Enum):
    """What kind of action a grant allows."""

    TOOL = "tool"  # delegate a named browser tool call
    SESSION = "session"  # delegate session-level verbs (create/close/observe)
    WORKFLOW = "workflow"  # delegate a named workflow run


# ---------------------------------------------------------------------------
# Policy / grants
# ---------------------------------------------------------------------------


class CapabilityGrant(BaseModel):
    """A single permission entry: what this peer may ask us to do."""

    capability: str = Field(..., description="e.g. 'tool:browser.click' or 'session:observe'")
    url_allowlist: list[str] = Field(default_factory=list, description="Allowed URL patterns (empty = unrestricted)")
    require_approval: bool = Field(False, description="Gate this capability behind operator approval")
    max_invocations_per_hour: int = Field(0, description="Rate limit (0 = unlimited)")
    expires_at: float = Field(0.0, description="Unix timestamp, 0 = never expires")


class PeerGrant(BaseModel):
    """All capabilities granted to one peer node."""

    node_id: str
    grants: list[CapabilityGrant] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Peer registry
# ---------------------------------------------------------------------------


class PeerRecord(BaseModel):
    """A registered peer node."""

    node_id: str
    display_name: str = ""
    pubkey_b64: str = Field(..., description="Base64-encoded Ed25519 public key (32 bytes → 44 chars)")
    endpoint: str = Field("", description="https://host:port — where to POST envelopes")
    grants: list[CapabilityGrant] = Field(default_factory=list)
    added_at: float = Field(default_factory=time.time)
    last_seen: float = 0.0


# ---------------------------------------------------------------------------
# Signed envelopes
# ---------------------------------------------------------------------------


class SignedEnvelope(BaseModel):
    """A tamper-evident message between mesh nodes."""

    sender_node_id: str
    recipient_node_id: str
    nonce: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = Field(default_factory=time.time)
    payload: dict[str, Any] = Field(default_factory=dict)
    signature_b64: str = ""  # filled by make_envelope()


# ---------------------------------------------------------------------------
# Delegation request / response
# ---------------------------------------------------------------------------


class DelegationRequest(BaseModel):
    """Sent by an orchestrator to ask a peer to execute a capability."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    capability: str = Field(..., description="e.g. 'tool:browser.click'")
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str = ""
    require_approval_advisory: bool = False  # hint from policy evaluator


class DelegationResponse(BaseModel):
    """Returned by the receiving peer after executing (or rejecting)."""

    request_id: str
    status: str  # "ok" | "rejected" | "approval_required" | "error"
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    approval_id: str = ""  # set when status == "approval_required"
