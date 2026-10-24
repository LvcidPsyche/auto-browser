"""Pillar 1 — Mesh routes (/mesh)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

mesh_router = APIRouter(prefix="/mesh", tags=["mesh"])


class MeshReceiveRequest(BaseModel):
    sender_node_id: str
    recipient_node_id: str
    nonce: str
    timestamp: float
    payload: dict[str, Any]
    signature_b64: str


@mesh_router.post("/receive")
async def mesh_receive(body: MeshReceiveRequest, request: Request):
    """
    Inbound delegation endpoint. Peers POST signed envelopes here.
    Calls DelegationManager.receive_inbound() and returns the reply envelope.
    """
    app = request.app
    delegation_mgr = getattr(app.state, "delegation_manager", None)
    if delegation_mgr is None:
        raise HTTPException(503, "Mesh not initialized")

    from app.mesh import DelegationRejected, DelegationReplayError, SignedEnvelope, make_envelope

    envelope = SignedEnvelope(**body.model_dump())
    try:
        response = await delegation_mgr.receive_inbound(envelope)
    except (DelegationRejected, DelegationReplayError):
        raise HTTPException(403, "Mesh delegation rejected")
    except Exception:
        logger.exception("mesh.receive error")
        raise HTTPException(500, "Mesh receive failed")

    identity = app.state.mesh_identity
    reply_payload = response.model_dump(mode="json")
    reply_envelope = make_envelope(
        identity=identity,
        payload=reply_payload,
        recipient_node_id=envelope.sender_node_id,
    )
    return reply_envelope.model_dump()


@mesh_router.get("/peers")
async def mesh_list_peers(request: Request):
    peers = getattr(request.app.state, "peer_registry", None)
    if peers is None:
        raise HTTPException(503, "Mesh not initialized")
    return {"peers": [p.model_dump() for p in peers.all()]}


# Peers are not administered over HTTP. /mesh/receive skips the bearer token and trusts any sender
# in the registry, so registering a peer grants it tool access: an API route for it would let any
# credential mint a standing, token-free way in that survives revoking that credential. The host
# operator edits the file MESH_PEERS_PATH names instead (see docs/mesh.md); it is re-read when it
# changes.


@mesh_router.get("/identity")
async def mesh_identity(request: Request):
    identity = getattr(request.app.state, "mesh_identity", None)
    if identity is None:
        raise HTTPException(503, "Mesh not initialized")
    return {"node_id": identity.node_id, "pubkey_b64": identity.pubkey_b64}
