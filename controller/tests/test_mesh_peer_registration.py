"""Mesh peers come only from the peers file, and /mesh/receive trusts nothing else.

/mesh/receive skips the bearer token and accepts any sender found in the peer
registry. POST /mesh/peers let any API credential add a sender, with
`tool:*` grants and any public key, so one ordinary credential could mint a
standing, token-free way into the tool gateway that outlived the credential
(and could replace or remove a real peer). Registering a peer is now a host
operation: the operator edits the file MESH_PEERS_PATH names, which is
re-read when it changes. These tests also pin what /mesh/receive demands of
the sender, because with the registration routes gone that check is the whole
boundary.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.mesh.delegation import DelegationManager
from app.mesh.identity import NodeIdentity
from app.mesh.models import CapabilityGrant, PeerRecord, SignedEnvelope
from app.mesh.peers import PeerRegistryFile
from app.mesh.transport import _canonical_bytes, make_envelope
from app.routes.extensions import _DASHBOARD_HTML, register_all_routers

OBSERVE = "tool:browser.observe"


@pytest.fixture()
def mesh(tmp_path: Path):
    server = NodeIdentity(tmp_path / "server")
    attacker = NodeIdentity(tmp_path / "attacker")
    legit = NodeIdentity(tmp_path / "legit")
    peers_path = tmp_path / "peers.json"
    peers = PeerRegistryFile(peers_path)
    calls: list[tuple[str, dict]] = []

    async def tool_gateway(tool_name: str, arguments: dict, session_id: str) -> dict:
        calls.append((tool_name, arguments))
        return {"tool": tool_name}

    app = FastAPI()
    app.state.settings = SimpleNamespace(operator_id_header="X-Operator-Id", operator_name_header="X-Operator-Name")
    app.state.mesh_identity = server
    app.state.peer_registry = peers
    app.state.delegation_manager = DelegationManager(identity=server, peers=peers, tool_gateway=tool_gateway)
    register_all_routers(app)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            app=app,
            server=server,
            attacker=attacker,
            legit=legit,
            path=peers_path,
            peers=peers,
            calls=calls,
        )


def add_legit_peer(mesh, *grants: str) -> None:
    mesh.peers.add(
        PeerRecord(
            node_id=mesh.legit.node_id,
            pubkey_b64=mesh.legit.pubkey_b64,
            grants=[CapabilityGrant(capability=capability) for capability in grants],
        )
    )


def request_payload(capability: str = OBSERVE) -> dict:
    return {"capability": capability, "arguments": {}, "session_id": ""}


def send(mesh, sender: NodeIdentity, capability: str = OBSERVE):
    envelope = make_envelope(sender, request_payload(capability), recipient_node_id=mesh.server.node_id)
    return mesh.client.post("/mesh/receive", json=envelope.model_dump())


def sign_as(identity: NodeIdentity, envelope: SignedEnvelope) -> dict:
    import base64

    envelope.signature_b64 = base64.b64encode(identity.sign(_canonical_bytes(envelope))).decode()
    return envelope.model_dump()


def reply_status(response) -> str:
    return response.json()["payload"]["status"]


# ── registration is not an API operation ────────────────────────────────────


def test_a_peer_cannot_be_registered_over_http(mesh) -> None:
    body = {
        "node_id": mesh.attacker.node_id,
        "pubkey_b64": mesh.attacker.pubkey_b64,
        "grants": [{"capability": "tool:*"}],
    }

    registered = mesh.client.post("/mesh/peers", json=body)

    assert registered.status_code in (404, 405)
    assert send(mesh, mesh.attacker).status_code == 403  # still an unknown sender
    assert mesh.attacker.node_id not in (mesh.path.read_text() if mesh.path.exists() else "")
    assert mesh.calls == []


def test_a_peer_cannot_be_removed_or_overwritten_over_http(mesh) -> None:
    add_legit_peer(mesh)

    removed = mesh.client.delete(f"/mesh/peers/{mesh.legit.node_id}")
    overwritten = mesh.client.post(
        "/mesh/peers",
        json={
            "node_id": mesh.legit.node_id,
            "pubkey_b64": mesh.attacker.pubkey_b64,
            "grants": [{"capability": "tool:*"}],
        },
    )

    assert removed.status_code in (404, 405)
    assert overwritten.status_code in (404, 405)
    kept = mesh.peers.get(mesh.legit.node_id)
    assert (kept.pubkey_b64, kept.grants) == (mesh.legit.pubkey_b64, [])


def test_the_only_mesh_routes_that_change_state_are_the_signed_receive(mesh) -> None:
    served = mesh.app.openapi()["paths"]
    writes = {
        (path, method.upper())
        for path, operations in served.items()
        if path.startswith("/mesh")
        for method in operations
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}
    }

    assert any(path.startswith("/mesh") for path in served)
    assert writes == {("/mesh/receive", "POST")}


def test_the_read_only_views_still_work(mesh) -> None:
    add_legit_peer(mesh, OBSERVE)

    peers = mesh.client.get("/mesh/peers").json()["peers"]
    identity = mesh.client.get("/mesh/identity").json()

    assert [peer["node_id"] for peer in peers] == [mesh.legit.node_id]
    assert identity["node_id"] == mesh.server.node_id


def test_the_dashboard_no_longer_offers_to_remove_a_peer(mesh) -> None:
    assert "removePeer" not in _DASHBOARD_HTML
    assert "method: 'DELETE'" not in _DASHBOARD_HTML.split("async function loadPeers()")[1].split("async function")[0]
    assert "/mesh/peers/" not in _DASHBOARD_HTML


# ── a peer added to the file is a peer; /mesh/receive asks the file ─────────


def test_a_peer_written_into_the_file_is_accepted_without_a_restart(mesh) -> None:
    assert send(mesh, mesh.legit).status_code == 403
    mesh.path.write_text(
        json.dumps(
            {
                mesh.legit.node_id: {
                    "node_id": mesh.legit.node_id,
                    "pubkey_b64": mesh.legit.pubkey_b64,
                    "grants": [{"capability": OBSERVE}],
                }
            }
        ),
        encoding="utf-8",
    )
    os.utime(mesh.path, (time.time() + 5, time.time() + 5))  # the registry reloads on a new mtime

    response = send(mesh, mesh.legit)

    assert response.status_code == 200
    assert reply_status(response) == "ok"
    assert mesh.calls == [("browser.observe", {})]


def test_removing_a_peer_from_the_file_revokes_it_at_once(mesh) -> None:
    add_legit_peer(mesh, OBSERVE)
    assert send(mesh, mesh.legit).status_code == 200

    mesh.path.write_text("{}", encoding="utf-8")
    os.utime(mesh.path, (time.time() + 5, time.time() + 5))

    assert send(mesh, mesh.legit).status_code == 403


# ── what /mesh/receive demands of the sender ────────────────────────────────


def test_an_unknown_sender_is_refused_even_with_a_valid_signature_of_its_own(mesh) -> None:
    response = send(mesh, mesh.attacker)

    assert response.status_code == 403
    assert mesh.calls == []


def test_a_registered_peer_needs_no_bearer_token_but_stays_inside_its_grants(mesh) -> None:
    add_legit_peer(mesh, OBSERVE)

    allowed = send(mesh, mesh.legit, OBSERVE)
    denied = send(mesh, mesh.legit, "tool:browser.eval_js")

    assert "authorization" not in {name.lower() for name in allowed.request.headers}
    assert (allowed.status_code, reply_status(allowed)) == (200, "ok")
    assert (denied.status_code, reply_status(denied)) == (200, "rejected")
    assert mesh.calls == [("browser.observe", {})]


def test_a_registered_node_id_signed_with_another_key_is_refused(mesh) -> None:
    add_legit_peer(mesh, "tool:*")
    forged = SignedEnvelope(
        sender_node_id=mesh.legit.node_id,
        recipient_node_id=mesh.server.node_id,
        payload=request_payload("tool:browser.list_sessions"),
    )

    response = mesh.client.post("/mesh/receive", json=sign_as(mesh.attacker, forged))

    assert response.status_code == 403
    assert mesh.calls == []


def test_a_payload_changed_after_signing_is_refused(mesh) -> None:
    add_legit_peer(mesh, "tool:*")
    envelope = make_envelope(mesh.legit, request_payload(OBSERVE), recipient_node_id=mesh.server.node_id)
    body = envelope.model_dump()
    body["payload"] = request_payload("tool:browser.list_sessions")

    response = mesh.client.post("/mesh/receive", json=body)

    assert response.status_code == 403
    assert mesh.calls == []


def test_an_envelope_addressed_to_another_node_is_refused(mesh) -> None:
    add_legit_peer(mesh, OBSERVE)
    envelope = make_envelope(mesh.legit, request_payload(), recipient_node_id=mesh.attacker.node_id)

    response = mesh.client.post("/mesh/receive", json=envelope.model_dump())

    assert response.status_code == 403
    assert mesh.calls == []


@pytest.mark.parametrize("offset", [-3600.0, 3600.0], ids=["stale", "future"])
def test_a_correctly_signed_envelope_outside_the_time_window_is_refused(mesh, offset) -> None:
    add_legit_peer(mesh, OBSERVE)
    envelope = SignedEnvelope(
        sender_node_id=mesh.legit.node_id,
        recipient_node_id=mesh.server.node_id,
        timestamp=time.time() + offset,
        payload=request_payload(),
    )

    response = mesh.client.post("/mesh/receive", json=sign_as(mesh.legit, envelope))

    assert response.status_code == 403
    assert mesh.calls == []


@pytest.mark.parametrize("stamp", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_timestamp_is_not_inside_the_window(mesh, stamp) -> None:
    add_legit_peer(mesh, OBSERVE)
    envelope = SignedEnvelope(
        sender_node_id=mesh.legit.node_id,
        recipient_node_id=mesh.server.node_id,
        timestamp=stamp,
        payload=request_payload(),
    )

    # httpx refuses to encode NaN/Infinity itself; a hostile client would not.
    response = mesh.client.post(
        "/mesh/receive",
        content=json.dumps(sign_as(mesh.legit, envelope)),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 403
    assert mesh.calls == []


def test_a_replayed_envelope_is_refused(mesh) -> None:
    add_legit_peer(mesh, OBSERVE)
    body = make_envelope(mesh.legit, request_payload(), recipient_node_id=mesh.server.node_id).model_dump()

    first = mesh.client.post("/mesh/receive", json=body)
    second = mesh.client.post("/mesh/receive", json=body)

    assert (first.status_code, second.status_code) == (200, 403)
    assert len(mesh.calls) == 1


def test_a_malformed_body_is_refused_before_any_peer_lookup(mesh) -> None:
    assert mesh.client.post("/mesh/receive", json={"sender_node_id": mesh.legit.node_id}).status_code == 422
    assert mesh.client.post("/mesh/receive", content=b"not json").status_code == 422
