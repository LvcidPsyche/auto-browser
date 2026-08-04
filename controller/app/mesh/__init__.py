"""
auto-browser mesh — multi-node peer delegation with Ed25519 envelope security.

Public surface (26 names):
"""

from .delegation import (
    ApprovalFn,
    DelegationError,
    DelegationManager,
    DelegationRejected,
    DelegationReplayError,
    ToolGatewayFn,
)
from .identity import NodeIdentity
from .models import (
    CapabilityGrant,
    CapabilityKind,
    DelegationRequest,
    DelegationResponse,
    PeerGrant,
    PeerRecord,
    SignedEnvelope,
)
from .peers import PeerRegistryFile
from .policy import (
    PolicyDenied,
    PolicyError,
    PolicyEvaluator,
    PolicyExpired,
    PolicyRateLimited,
)
from .transport import (
    EnvelopeVerificationError,
    TransportError,
    make_envelope,
    send_envelope,
    verify_envelope,
)

__all__ = [
    # delegation
    "DelegationError",
    "DelegationManager",
    "DelegationRejected",
    "DelegationReplayError",
    "ToolGatewayFn",
    "ApprovalFn",
    # identity
    "NodeIdentity",
    # models
    "CapabilityGrant",
    "CapabilityKind",
    "DelegationRequest",
    "DelegationResponse",
    "PeerGrant",
    "PeerRecord",
    "SignedEnvelope",
    # peers
    "PeerRegistryFile",
    # policy
    "PolicyDenied",
    "PolicyError",
    "PolicyEvaluator",
    "PolicyExpired",
    "PolicyRateLimited",
    # transport
    "EnvelopeVerificationError",
    "TransportError",
    "make_envelope",
    "send_envelope",
    "verify_envelope",
]
