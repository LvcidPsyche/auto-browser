"""A mesh peer's own strings cannot start a line of the controller's log.

A registered peer signs its request, and the capability and the session id in it are the peer's text: the capability
whole when no grant matches it, and past the prefix under a wildcard grant (``tool:*``). Logged with %s, a line break in
either began a line of the peer's choosing. Each of these log calls writes them quoted and cut now (quoted_for_log), as
the refused request's path is (tests/test_http_middleware.py): %r escaped them, but wrote them as long as the body.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import pytest

from app.action_errors import SessionNotFoundError
from app.browser.services.sessions import BrowserSessionService
from app.mesh.delegation import DelegationManager, DelegationRejected
from app.mesh.identity import NodeIdentity
from app.mesh.models import CapabilityGrant, PeerRecord
from app.mesh.peers import PeerRegistryFile
from app.mesh.transport import make_envelope
from app.session_store import DurableSessionStore, FileSessionStore
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


def test_a_request_that_does_not_decode_is_refused_without_the_values_it_held(mesh) -> None:
    # The refusal's text was pydantic's own, which repeats each value it refused (the first and last characters of a
    # long one). The route answers a fixed 403; the text is the DelegationRejected's, for whatever takes that.
    add_legit_peer(mesh, "tool:*")
    payload = {"capability": ["a value that must not come back"], "arguments": {}, "session_id": ""}
    envelope = make_envelope(mesh.legit, payload, recipient_node_id=mesh.server.node_id)

    with pytest.raises(DelegationRejected) as refused:
        asyncio.run(mesh.app.state.delegation_manager.receive_inbound(envelope))
    response = mesh.client.post("/mesh/receive", json=envelope.model_dump())

    assert str(refused.value) == "Malformed DelegationRequest: 1 refused by DelegationRequest: capability: string_type"
    assert (response.status_code, response.json()) == (403, {"detail": "Mesh delegation rejected"})
    assert mesh.calls == []


# What of a refused URL is not for a log: its userinfo, its path, its query and its fragment.
NOT_FOR_THE_LOG = ("alice", "hunter2", "private-path", "SECRET-TOKEN-123")
REFUSED_URLS = {
    "userinfo": (
        "https://alice:hunter2@example.com/private-path?token=SECRET-TOKEN-123",
        "host 'example.com' that carries userinfo",
    ),
    # A user name is userinfo with no password beside it, and the URL is refused for it all the same.
    "a user name and no password": (
        "https://alice@example.com/private-path?token=SECRET-TOKEN-123",
        "host 'example.com' that carries userinfo",
    ),
    "another host": (
        "https://evil.example/private-path?token=SECRET-TOKEN-123#hunter2",
        "A URL of host 'evil.example' is",
    ),
    # Read as the browser reads it, which is how the allowlist judged it: urllib finds no host name in this one.
    "backslashes where the slashes are": (
        "https:\\\\evil.example\\private-path?token=SECRET-TOKEN-123",
        "A URL of host 'evil.example' is",
    ),
    "a port that is none": (
        "https://evil.example:99999999/private-path?token=SECRET-TOKEN-123",
        "A URL of host 'evil.example' and a port that cannot be read, which no pattern allows, is",
    ),
    # The host alone is one the pattern allows: named with no reason beside it, the refusal read as a mistake.
    "an allowed host and a port that is none": (
        "https://example.com:99999/private-path?token=SECRET-TOKEN-123",
        "A URL of host 'example.com' and a port that cannot be read, which no pattern allows, is",
    ),
    "an allowed host and a port that is no number": (
        "https://example.com:abc/private-path?token=SECRET-TOKEN-123",
        "A URL of host 'example.com' and a port that cannot be read, which no pattern allows, is",
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


# A host a pattern allows, in a URL that another part keeps out: named with no reason beside it, the refusal read as a
# mistake. What follows the host in the line, for each.
PATTERNS = ["https://*.example.com/api/*", "*.example.org"]
REFUSED_PARTS = {
    "a port": (PATTERNS, "https://a.example.com:8443/api/private-path", "and a port no pattern allows with that host,"),
    # The authority is matched with its port as written: the scheme's own, written out, is a port the pattern lacks.
    "the scheme's own port written out": (
        PATTERNS,
        "https://a.example.com:443/api/private-path",
        "and a port no pattern allows with that host,",
    ),
    "a scheme": (PATTERNS, "http://a.example.com/api/private-path", "and a scheme no pattern allows with that host,"),
    "a path": (PATTERNS, "https://a.example.com/private-path", "and a path no pattern allows with that host,"),
    "a path and a query": (
        PATTERNS,
        "https://a.example.com/private-path?token=SECRET-TOKEN-123",
        "and a path (with its query or fragment) no pattern allows with that host,",
    ),
    "a scheme and a path": (
        PATTERNS,
        "http://a.example.com/private-path",
        "and a scheme and path no pattern allows with that host,",
    ),
    "a scheme, a port and a path": (
        PATTERNS,
        "http://a.example.com:8443/private-path",
        "and a scheme, port and path no pattern allows with that host,",
    ),
    "two patterns of the host, each refusing another part": (
        ["https://*.example.com/api/*", "http://*.example.com/*"],
        "https://a.example.com/private-path",
        "with a scheme, port and path no one pattern allows together,",
    ),
    # A URL that writes no port, of a host whose patterns each name one: "a port no pattern allows" named a port the
    # URL does not write, and with :443 the very port the pattern allows.
    "a pattern with a port of its own": (
        ["https://*.example.com:8443/*"],
        "https://a.example.com/private-path",
        "and no port written, where each pattern of that host names one,",
    ),
    "a pattern with the scheme's own port written out": (
        ["https://*.example.com:443/*"],
        "https://a.example.com/x",
        "and no port written, where each pattern of that host names one,",
    ),
    "a scheme, and a pattern with a port of its own": (
        ["https://*.example.com:443/*"],
        "http://a.example.com/x",
        "and a scheme no pattern allows with that host, and no port written, where each pattern of that host names one,",
    ),
    # An IPv6 host a pattern allows: the pattern's host is read up to its bracket, and the port is what it refuses.
    "an IPv6 host and a port": (
        ["https://[::1]/*"],
        "https://[::1]:9000/x",
        "and a port no pattern allows with that host,",
    ),
    # A pattern written with white space round it is read as the same pattern without, as the allowlist reads it.
    "a path, of a pattern with white space round it": (
        ["  https://*.example.com/api/*  "],
        "https://a.example.com/private-path",
        "and a path no pattern allows with that host,",
    ),
}


@pytest.mark.parametrize(("patterns", "url", "named_as"), REFUSED_PARTS.values(), ids=REFUSED_PARTS.keys())
def test_a_refused_url_whose_host_a_pattern_allows_says_what_no_pattern_allows(
    mesh, caplog: pytest.LogCaptureFixture, patterns: list[str], url: str, named_as: str
) -> None:
    grant = CapabilityGrant(capability="tool:*", url_allowlist=patterns)
    mesh.peers.add(PeerRecord(node_id=mesh.legit.node_id, pubkey_b64=mesh.legit.pubkey_b64, grants=[grant]))
    caplog.set_level(logging.INFO)

    response = _post(mesh, "tool:browser.navigate", arguments={"url": url})
    host_not_allowed = _post(mesh, "tool:browser.navigate", arguments={"url": "https://evil.example/private-path"})

    assert reply_status(response) == reply_status(host_not_allowed) == "rejected"
    messages = [record.getMessage() for record in caplog.records if record.name == "app.mesh.delegation"]
    lines = [message for message in messages if message.startswith("mesh.policy.denied")]
    assert len(lines) == 2, lines
    assert f"A URL of host {urlsplit(url).hostname!r} {named_as} is not in the allowlist" in lines[0], lines[0]
    # A host no pattern allows is named as it was, with nothing beside it.
    assert "A URL of host 'evil.example' is not in the allowlist" in lines[1], lines[1]
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


def test_a_refused_urls_host_of_any_length_is_cut_in_the_line(mesh, caplog: pytest.LogCaptureFixture) -> None:
    # A host name is as long as the body cap lets a request be, and the line is written at every refusal.
    grant = CapabilityGrant(capability="tool:*", url_allowlist=["https://example.com/*"])
    mesh.peers.add(PeerRecord(node_id=mesh.legit.node_id, pubkey_b64=mesh.legit.pubkey_b64, grants=[grant]))
    caplog.set_level(logging.INFO)

    response = _post(mesh, "tool:browser.navigate", arguments={"url": "https://" + "h" * 3000 + ".example/"})

    assert reply_status(response) == "rejected"
    messages = [record.getMessage() for record in caplog.records if record.name == "app.mesh.delegation"]
    (line,) = [message for message in messages if message.startswith("mesh.policy.denied")]
    # Cut at 200 characters, the host in the refusal and then the refusal in the line, each cut mark after the
    # closing quote: the line writes the error's text quoted and cut, as every line that logs an error does.
    assert "reason=\"A URL of host 'hhhh" in line, line[:300]
    assert re.search('h"[.][.][.] [(]cut from [0-9]+ characters[)]$', line), line[-80:]
    assert len(line) < 600, len(line)


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
    # Quoted, so the cut mark comes after a closing quote: cut unquoted, the field's own quote was cut with it.
    assert "error=\"recipient_node_id 'rxxx" in line, line[:200]
    assert re.search('x"[.][.][.] [(]cut from [0-9]+ characters[)]$', line), line[-80:]
    assert len(line) < 500


def test_an_error_the_receive_route_meets_is_one_record_with_the_peers_text_quoted_and_cut(
    mesh, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The route takes no credential, and wrote what it did not expect by logger.exception: a traceback that prints
    # the error's text as it is, and the error can quote what the peer sent. A line break, an ESC, 5,000 characters.
    peers_text = "tool:x\nWARNING:app.main:forged by a peer\x1b[2J" + "x" * 5000
    failed = AsyncMock(side_effect=RuntimeError("no grant for " + peers_text))
    monkeypatch.setattr(mesh.app.state.delegation_manager, "receive_inbound", failed)
    caplog.set_level(logging.INFO)

    response = _post_unsigned(mesh, mesh.server.node_id)

    assert (response.status_code, response.json()) == (500, {"detail": "Mesh receive failed"})
    (record,) = [record for record in caplog.records if record.name == "app.routes.extensions.mesh"]
    assert (record.levelno, record.exc_info) == (logging.ERROR, None)
    first, *where = record.getMessage().splitlines()
    assert first.startswith("mesh.receive failed: answered 500 for RuntimeError: 'no grant for tool:x\\nWARNING:"), (
        first
    )
    assert "\x1b" not in record.getMessage() and len(first) < 400, first[:120]
    assert where and all(line.startswith("  ") and "forged" not in line for line in where), where


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


# A peer's text as long as its signed body lets it be: %r escaped it, and wrote it whole.
LONG = "y" * 60_000


@pytest.mark.parametrize(
    ("grants", "capability", "session_id", "logger_name", "starts"),
    [
        ((), "tool:x" + LONG, "", "app.mesh.delegation", "mesh.policy.denied"),
        (("tool:*",), "tool:x" + LONG, "", "app.mesh.policy", "mesh.policy: PERMIT"),
        (("session:*",), "session:close" + LONG, "", "app.mesh.delegation", "mesh.delegation.session_verb"),
        (("session:close",), "session:close", "s1" + LONG, "app.mesh.delegation", "mesh.delegation.session_verb"),
    ],
    ids=["no grant", "a wildcard grant", "a session verb", "a session id"],
)
def test_a_peers_text_of_60000_characters_is_cut(
    mesh, caplog: pytest.LogCaptureFixture, grants, capability, session_id, logger_name, starts
) -> None:
    add_legit_peer(mesh, *grants)
    caplog.set_level(logging.INFO)

    _post(mesh, capability, session_id=session_id)

    messages = [record.getMessage() for record in caplog.records if record.name == logger_name]
    (message,) = [message for message in messages if message.startswith(starts)]
    # Each text quoted and cut at 200 characters, the denial's reason too (it quotes the capability).
    assert len(message) < 800, (len(message), message[:300])
    assert "'tool:xyyy" in message or "'closeyyy" in message or "'s1yyy" in message, message[:300]


def test_the_two_approval_lines_cut_a_peers_text_of_60000_characters(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    server, legit = NodeIdentity(tmp_path / "server"), NodeIdentity(tmp_path / "legit")
    peers = PeerRegistryFile(tmp_path / "peers.json")
    grant = CapabilityGrant(capability="tool:*", require_approval=True)
    peers.add(PeerRecord(node_id=legit.node_id, pubkey_b64=legit.pubkey_b64, grants=[grant]))

    async def denied(approval_id: str, request: dict) -> dict:
        return {"status": "denied"}

    manager = DelegationManager(identity=server, peers=peers, approval_fn=denied)
    manager._started_at -= manager._timestamp_window + 1  # up for longer than one window
    envelope = make_envelope(legit, {"capability": "tool:x" + LONG}, recipient_node_id=server.node_id)
    caplog.set_level(logging.INFO)

    assert asyncio.run(manager.receive_inbound(envelope)).status == "rejected"

    lines = [record.getMessage() for record in caplog.records if record.name == "app.mesh.delegation"]
    for starts in ("mesh.delegation.approval_required", "mesh.delegation.approval_denied"):
        (message,) = [line for line in lines if line.startswith(starts)]
        assert len(message) < 500 and "capability='tool:xyyy" in message, (len(message), message[:300])


def test_a_session_id_that_names_no_record_is_logged_on_one_line_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A tool that looks a session up hands the service the id it was sent: a mesh peer's under a grant for such a
    # tool, or an MCP caller's. With LOG_LEVEL=DEBUG, which .env.example offers, the lookup that finds no record it
    # can read logs the id. The file store refuses an id that is no plain name. The id was written uncut (%r), and the
    # traceback under it repeated it whole in the store's KeyError: a record held a 60,000-character id twice.
    manager = SimpleNamespace(sessions={}, session_store=FileSessionStore(tmp_path))
    caplog.set_level(logging.DEBUG)
    long_id = "s1" + FORGED + "x" * 60_000

    with pytest.raises(SessionNotFoundError):
        asyncio.run(BrowserSessionService(manager).get(long_id))

    message = _the_line(caplog, "app.browser.services.sessions", "no readable record for session")
    # As a handler writes it: the id quoted and cut, the error by its class alone, and nothing under it.
    (record,) = [record for record in caplog.records if record.getMessage() == message]
    assert record.exc_info is None
    assert logging.Formatter().format(record) == message
    assert message == f"no readable record for session {long_id[:200]!r}...: KeyError", ascii(message[-80:])


@pytest.mark.parametrize(
    "failure",
    [ConnectionError("Error 10061 connecting to 127.0.0.1:6379"), RuntimeError("MASTERDOWN Link with MASTER is down")],
    ids=["redis gone", "redis answering an error"],
)
def test_a_session_id_that_is_no_plain_name_is_refused_before_redis_is_asked(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: Exception
) -> None:
    # With REDIS_URL set, the lookup asked Redis first, which takes any text as its key, and while its read failed
    # the store's WARNING wrote the id as it came, whatever LOG_LEVEL is: from every route and tool that looks a
    # session up. The file store refuses such an id before it touches the disk.
    store = DurableSessionStore(file_root=tmp_path, redis_url=None, redis_prefix="auto-browser:test")
    asyncio.run(store.startup())
    redis = Mock()
    redis.get = AsyncMock(side_effect=failure)
    store.redis_store = redis
    store._primary = redis  # what startup() does when Redis answers
    manager = SimpleNamespace(sessions={}, session_store=store)
    caplog.set_level(logging.DEBUG)

    # Refused as without REDIS_URL, before Redis is asked.
    with pytest.raises(SessionNotFoundError):
        asyncio.run(BrowserSessionService(manager).get("s1" + FORGED))
    redis.get.assert_not_awaited()
    # A plain name is still asked of Redis first, and the line that says its read failed quotes it.
    with pytest.raises(SessionNotFoundError):
        asyncio.run(BrowserSessionService(manager).get("s1"))
    redis.get.assert_awaited_once_with("s1")

    assert "redis session read failed for 's1', using file record" in caplog.text
    written = [line for record in caplog.records for line in logging.Formatter().format(record).splitlines()]
    assert [line for line in written if line.startswith("WARNING:app.auth")] == [], ascii(written)
