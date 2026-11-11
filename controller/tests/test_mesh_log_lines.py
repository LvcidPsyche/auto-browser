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
from types import SimpleNamespace

import pytest

from app.action_errors import SessionNotFoundError
from app.browser.services.sessions import BrowserSessionService
from app.mesh.delegation import DelegationManager
from app.mesh.identity import NodeIdentity
from app.mesh.models import CapabilityGrant, PeerRecord
from app.mesh.peers import PeerRegistryFile
from app.mesh.transport import make_envelope
from app.session_store import FileSessionStore
from tests import test_mesh_peer_registration as _registration
from tests.test_mesh_peer_registration import add_legit_peer, reply_status, sign_as

mesh = _registration.mesh  # the route fixture: a server, a legit identity, the peers file and a test client

# Every character str.splitlines() ends a line at, after a line a reader of the log would take for the controller's.
FORGED = "\nWARNING:app.auth:forged by a peer" + "\r\x0b\x0c\x1c\x1d\x1e\x85  " + "end"


def _post(mesh, capability: str, session_id: str = "", arguments: dict | None = None):
    payload = {"capability": capability, "arguments": arguments or {}, "session_id": session_id}
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


# What of a refused URL is not for a log: its userinfo, its path, its query and its fragment.
NOT_FOR_THE_LOG = ("alice", "hunter2", "private-path", "SECRET-TOKEN-123")
REFUSED_URLS = {
    "userinfo": (
        "https://alice:hunter2@example.com/private-path?token=SECRET-TOKEN-123",
        "host 'example.com' that carries userinfo",
    ),
    "another host": (
        "https://evil.example/private-path?token=SECRET-TOKEN-123#hunter2",
        "A URL of host 'evil.example' is",
    ),
    "a port that is none": (
        "https://evil.example:99999999/private-path?token=SECRET-TOKEN-123",
        "A URL of host 'evil.example' is",
    ),
    "no host name": ("https://:hunter2@/private-path?token=SECRET-TOKEN-123", "A URL with no host name is"),
    "not a URL": ("https://[::1/private-path?token=SECRET-TOKEN-123", "A URL that cannot be read is"),
}


@pytest.mark.parametrize(("url", "named_as"), REFUSED_URLS.values(), ids=REFUSED_URLS.keys())
def test_a_url_the_allowlist_refuses_is_logged_by_its_host_alone(
    mesh, caplog: pytest.LogCaptureFixture, url: str, named_as: str
) -> None:
    # The reason of a refusal is logged, and it quoted the URL as the peer sent it. A URL with userinfo is refused
    # for having it, so that refusal was what wrote a password to the log.
    grant = CapabilityGrant(capability="tool:*", url_allowlist=["https://example.com/*"])
    mesh.peers.add(PeerRecord(node_id=mesh.legit.node_id, pubkey_b64=mesh.legit.pubkey_b64, grants=[grant]))
    caplog.set_level(logging.INFO)

    response = _post(mesh, "tool:browser.navigate", arguments={"action": {"url": url}})

    assert reply_status(response) == "rejected"
    assert response.json()["payload"]["error"] == "Delegation policy rejected the request"
    messages = [record.getMessage() for record in caplog.records if record.name == "app.mesh.delegation"]
    (line,) = [message for message in messages if message.startswith("mesh.policy.denied")]
    assert named_as in line and "capability 'tool:*'" in line, line
    assert [part for part in NOT_FOR_THE_LOG if part in line] == [], line
    assert [part for part in NOT_FOR_THE_LOG if part in caplog.text] == [], caplog.text


def test_a_refused_urls_host_is_logged_on_one_line(mesh, caplog: pytest.LogCaptureFixture) -> None:
    # A host name is the peer's text too: only a tab, a line feed and a carriage return are taken out of a URL.
    grant = CapabilityGrant(capability="tool:*", url_allowlist=["https://example.com/*"])
    mesh.peers.add(PeerRecord(node_id=mesh.legit.node_id, pubkey_b64=mesh.legit.pubkey_b64, grants=[grant]))
    caplog.set_level(logging.INFO)

    response = _post(
        mesh, "tool:browser.navigate", arguments={"url": "https://evil.example\x0bforged by a peer\x1c\x85/"}
    )

    assert reply_status(response) == "rejected"
    _the_line(caplog, "app.mesh.delegation", "mesh.policy.denied")


def _post_unsigned(mesh, recipient: str, named: NodeIdentity | None = None):
    """An envelope that names a registered peer (the legit one, unless ``named``) as its sender, signed with a key
    that is not that peer's.

    What a caller with no credential can send: a peer's id is no secret, and the route takes no token.
    """
    forged = make_envelope(mesh.attacker, {"capability": "tool:x"}, recipient_node_id=recipient)
    forged.sender_node_id = (named or mesh.legit).node_id
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


def test_each_peer_named_has_a_window_of_its_own(mesh, caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    # One window for every sender would pass the test above: envelopes that name one peer would then hold back the
    # line of the first that names another.
    add_legit_peer(mesh)
    second = NodeIdentity(tmp_path / "second")
    mesh.peers.add(PeerRecord(node_id=second.node_id, pubkey_b64=second.pubkey_b64, grants=[]))
    caplog.set_level(logging.INFO)

    for _ in range(3):
        assert _post_unsigned(mesh, mesh.server.node_id).status_code == 403
    for _ in range(2):
        assert _post_unsigned(mesh, mesh.server.node_id, named=second).status_code == 403

    lines = _invalid_envelope_lines(caplog)
    assert [line.split()[1] for line in lines] == [f"sender={mesh.legit.node_id}", f"sender={second.node_id}"], lines


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


def test_a_session_id_that_names_no_record_is_logged_on_one_line_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A tool that looks a session up hands the service the id it was sent: a mesh peer's under a grant for such a
    # tool, or an MCP caller's. With LOG_LEVEL=DEBUG, which .env.example offers, the lookup that finds no record it
    # can read logs the id. The file store refuses an id that is no plain name.
    manager = SimpleNamespace(sessions={}, session_store=FileSessionStore(tmp_path))
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SessionNotFoundError):
        asyncio.run(BrowserSessionService(manager).get("s1" + FORGED))

    message = _the_line(caplog, "app.browser.services.sessions", "no readable record for session")
    # As a handler writes it, with the traceback the call asks for under the message: no line of it is the peer's.
    (record,) = [record for record in caplog.records if record.getMessage() == message]
    written = logging.Formatter().format(record).splitlines()
    assert written[0] == message
    assert [line for line in written if line.startswith("WARNING:")] == [], ascii(written)
