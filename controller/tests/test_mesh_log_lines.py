"""A mesh peer's own strings cannot start a line of the controller's log.

A registered peer signs its request, and the capability and the session id in it are the peer's text: the capability
whole when no grant matches it, and past the prefix under a wildcard grant (``tool:*``). Logged with %s, a line break in
either began a line of the peer's choosing. Each of these log calls writes them with %r now, as the refused request's
path is (tests/test_http_middleware.py).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from app.mesh.delegation import DelegationManager
from app.mesh.identity import NodeIdentity
from app.mesh.models import CapabilityGrant, PeerRecord
from app.mesh.peers import PeerRegistryFile
from app.mesh.transport import make_envelope
from tests import test_mesh_peer_registration as _registration
from tests.test_mesh_peer_registration import add_legit_peer, reply_status, sign_as

mesh = _registration.mesh  # the route fixture: a server, a legit identity, the peers file and a test client

# Every character str.splitlines() ends a line at, after a line a reader of the log would take for the controller's.
FORGED = "\nWARNING:app.auth:forged by a peer" + "\r\x0b\x0c\x1c\x1d\x1e\x85  " + "end"


def _post(mesh, capability: str, session_id: str = ""):
    payload = {"capability": capability, "arguments": {}, "session_id": session_id}
    envelope = make_envelope(mesh.legit, payload, recipient_node_id=mesh.server.node_id)
    return mesh.client.post("/mesh/receive", json=envelope.model_dump())


def _the_line(caplog: pytest.LogCaptureFixture, logger: str, starts_with: str) -> str:
    """The one record of ``logger`` that starts with ``starts_with``: it holds the peer's text, and is one line."""
    messages = [record.getMessage() for record in caplog.records if record.name == logger]
    (message,) = [message for message in messages if message.startswith(starts_with)]
    assert "forged by a peer" in message
    assert message.splitlines() == [message], ascii(message)
    return message


def test_a_capability_no_grant_matches_is_logged_on_one_line(mesh, caplog: pytest.LogCaptureFixture) -> None:
    add_legit_peer(mesh)  # registered, and granted nothing: any peer can ask for what it is refused
    caplog.set_level(logging.INFO)

    response = _post(mesh, "tool:x" + FORGED)

    assert reply_status(response) == "rejected"
    _the_line(caplog, "app.mesh.delegation", "mesh.policy.denied")


def test_a_capability_a_wildcard_grant_permits_is_logged_on_one_line(mesh, caplog: pytest.LogCaptureFixture) -> None:
    add_legit_peer(mesh, "tool:*")
    caplog.set_level(logging.INFO)

    response = _post(mesh, "tool:x" + FORGED)

    assert reply_status(response) == "ok"
    _the_line(caplog, "app.mesh.policy", "mesh.policy: PERMIT")


def test_a_session_verb_is_logged_on_one_line(mesh, caplog: pytest.LogCaptureFixture) -> None:
    add_legit_peer(mesh, "session:*")
    caplog.set_level(logging.INFO)

    _post(mesh, "session:close" + FORGED)

    _the_line(caplog, "app.mesh.delegation", "mesh.delegation.session_verb")


def test_a_session_id_is_logged_on_one_line(mesh, caplog: pytest.LogCaptureFixture) -> None:
    # The session id is the peer's text whatever the grant: an exact one is enough.
    add_legit_peer(mesh, "session:close")
    caplog.set_level(logging.INFO)

    _post(mesh, "session:close", session_id="s1" + FORGED)

    _the_line(caplog, "app.mesh.delegation", "mesh.delegation.session_verb")


def _post_unsigned(mesh, recipient: str):
    """An envelope that names the registered peer as its sender, signed with a key that is not that peer's.

    What a caller with no credential can send: a peer's id is no secret, and the route takes no token.
    """
    forged = make_envelope(mesh.attacker, {"capability": "tool:x"}, recipient_node_id=recipient)
    forged.sender_node_id = mesh.legit.node_id
    return mesh.client.post("/mesh/receive", json=sign_as(mesh.attacker, forged))


def _invalid_envelope_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    messages = [record.getMessage() for record in caplog.records if record.name == "app.mesh.delegation"]
    return [message for message in messages if message.startswith("mesh.delegation.envelope_invalid")]


def test_envelopes_nobody_signed_write_one_line_a_window_for_the_peer_they_name(
    mesh, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_legit_peer(mesh)
    caplog.set_level(logging.INFO)

    for _ in range(50):
        assert _post_unsigned(mesh, mesh.server.node_id).status_code == 403

    assert len(_invalid_envelope_lines(caplog)) == 1
    monkeypatch.setattr("app.utils.REFUSAL_LOG_WINDOW_SECONDS", 0)  # the window is over
    assert _post_unsigned(mesh, mesh.server.node_id).status_code == 403
    lines = _invalid_envelope_lines(caplog)
    assert len(lines) == 2
    assert "more" not in lines[0]
    assert "49 more" in lines[1]


def test_an_invalid_envelopes_line_is_cut_whatever_the_envelope_holds(mesh, caplog: pytest.LogCaptureFixture) -> None:
    # The error quotes a field of the envelope, as long as the body cap allows: a megabyte by default.
    add_legit_peer(mesh)
    caplog.set_level(logging.INFO)

    assert _post_unsigned(mesh, "r" + "x" * 100_000).status_code == 403

    (line,) = _invalid_envelope_lines(caplog)
    assert "recipient_node_id" in line
    assert len(line) < 500


def test_the_two_approval_lines_are_one_line_each(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The shipped start passes no approval handler, so these two are reached only by a caller of the class."""
    server, legit = NodeIdentity(tmp_path / "server"), NodeIdentity(tmp_path / "legit")
    peers = PeerRegistryFile(tmp_path / "peers.json")
    grant = CapabilityGrant(capability="tool:*", require_approval=True)
    peers.add(PeerRecord(node_id=legit.node_id, pubkey_b64=legit.pubkey_b64, grants=[grant]))

    async def denied(approval_id: str, request: dict) -> dict:
        return {"status": "denied"}

    manager = DelegationManager(identity=server, peers=peers, approval_fn=denied)
    manager._started_at -= manager._timestamp_window + 1  # up for longer than one window
    envelope = make_envelope(legit, {"capability": "tool:x" + FORGED}, recipient_node_id=server.node_id)
    caplog.set_level(logging.INFO)

    response = asyncio.run(manager.receive_inbound(envelope))

    assert response.status == "rejected"
    _the_line(caplog, "app.mesh.delegation", "mesh.delegation.approval_required")
    _the_line(caplog, "app.mesh.delegation", "mesh.delegation.approval_denied")
