"""Pillar 1 — Mesh routes (/mesh)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from .._utils import internal_error

logger = logging.getLogger(__name__)

mesh_router = APIRouter(prefix="/mesh", tags=["mesh"])

# Used when the settings carry no MESH_MAX_BODY_BYTES (a bare app in tests).
_DEFAULT_MAX_BODY_BYTES = 1024 * 1024


class MeshReceiveRequest(BaseModel):
    sender_node_id: str
    recipient_node_id: str
    nonce: str
    timestamp: float
    payload: dict[str, Any]
    signature_b64: str


async def _read_capped_body(request: Request, limit: int) -> bytes:
    """The request body, refused with 413 past `limit` bytes: by its Content-Length, then as it is read."""
    declared = request.headers.get("content-length")
    if declared is None or not declared.isascii() or not declared.isdigit() or int(declared) > limit:
        raise HTTPException(413, "Mesh envelope too large or of unknown length (send Content-Length)")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, "Mesh envelope too large")
        chunks.append(chunk)
    return b"".join(chunks)


@mesh_router.post(
    "/receive",
    # The body is read by hand (capped before it is parsed), so FastAPI sees no body model: declare it for /docs.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": MeshReceiveRequest.model_json_schema()}},
        }
    },
)
async def mesh_receive(request: Request):
    """
    Inbound delegation endpoint. Peers POST signed envelopes here.
    Calls DelegationManager.receive_inbound() and returns the reply envelope.

    The route takes no bearer token, so it reads at most MESH_MAX_BODY_BYTES (1 MiB by default) before it
    parses anything or looks at the sender.
    """
    app = request.app
    delegation_mgr = getattr(app.state, "delegation_manager", None)
    if delegation_mgr is None:
        raise HTTPException(503, "Mesh not initialized")

    limit = int(getattr(getattr(app.state, "settings", None), "mesh_max_body_bytes", _DEFAULT_MAX_BODY_BYTES))
    raw = await _read_capped_body(request, limit)
    try:
        body = MeshReceiveRequest.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(422, "Invalid mesh envelope") from None

    from app.mesh import DelegationRejected, DelegationReplayError, MeshReplayCacheFull, SignedEnvelope, make_envelope

    envelope = SignedEnvelope(**body.model_dump())
    try:
        response = await delegation_mgr.receive_inbound(envelope)
    except MeshReplayCacheFull:
        raise HTTPException(503, "mesh replay cache is full; retry later")
    except (DelegationRejected, DelegationReplayError):
        raise HTTPException(403, "Mesh delegation rejected")
    except Exception:
        # As a route's 500 is logged: the route takes no credential, and a traceback prints the error's text as it
        # is, which can quote what the peer sent.
        raise internal_error(logger, "mesh.receive failed", detail="Mesh receive failed") from None

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
