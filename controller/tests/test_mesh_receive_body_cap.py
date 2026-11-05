"""/mesh/receive skips the bearer token, so it must not buffer an unbounded body before it checks the sender."""

from __future__ import annotations

import json

from tests import test_mesh_peer_registration as base
from tests.test_mesh_peer_registration import OBSERVE, add_legit_peer, request_payload, send

mesh = base.mesh  # the same fixture: a server, a legit identity, the peers file and a test client

CAP = 4096


def _big_envelope(mesh, size: int) -> bytes:
    body = {
        "sender_node_id": mesh.legit.node_id,
        "recipient_node_id": mesh.server.node_id,
        "nonce": "n1",
        "timestamp": 1.0,
        "payload": {"padding": "x" * size},
        "signature_b64": "ZmFrZQ==",
    }
    return json.dumps(body).encode("utf-8")


def test_a_declared_length_over_the_cap_is_refused_with_413(mesh) -> None:
    mesh.app.state.settings.mesh_max_body_bytes = CAP

    response = mesh.client.post(
        "/mesh/receive", content=_big_envelope(mesh, CAP * 4), headers={"content-type": "application/json"}
    )

    assert response.status_code == 413
    assert mesh.calls == []


def test_a_body_with_no_content_length_is_refused_with_413(mesh) -> None:
    mesh.app.state.settings.mesh_max_body_bytes = CAP

    def chunks():
        yield _big_envelope(mesh, 10)

    response = mesh.client.post("/mesh/receive", content=chunks(), headers={"content-type": "application/json"})

    assert response.status_code == 413


def test_a_body_longer_than_its_declared_length_is_cut_off_at_the_cap(mesh) -> None:
    mesh.app.state.settings.mesh_max_body_bytes = CAP
    body = _big_envelope(mesh, CAP * 4)

    response = mesh.client.post(
        "/mesh/receive", content=body, headers={"content-type": "application/json", "content-length": str(CAP - 1)}
    )

    assert response.status_code == 413


def test_an_uninitialised_mesh_answers_503_before_reading_the_body(mesh) -> None:
    mesh.app.state.delegation_manager = None
    mesh.app.state.settings.mesh_max_body_bytes = CAP

    response = mesh.client.post(
        "/mesh/receive", content=_big_envelope(mesh, CAP * 4), headers={"content-type": "application/json"}
    )

    assert response.status_code == 503


def test_a_malformed_envelope_under_the_cap_is_a_422(mesh) -> None:
    response = mesh.client.post(
        "/mesh/receive", content=b'{"sender_node_id": 1}', headers={"content-type": "application/json"}
    )

    assert response.status_code == 422


def test_a_normal_signed_envelope_still_works_under_the_default_cap(mesh) -> None:
    add_legit_peer(mesh, OBSERVE)

    response = send(mesh, mesh.legit)

    assert response.status_code == 200
    assert request_payload()["capability"] == OBSERVE
    assert mesh.calls == [(OBSERVE.removeprefix("tool:"), {})]
