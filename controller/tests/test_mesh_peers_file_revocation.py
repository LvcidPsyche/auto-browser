"""The peers file is the only way to revoke a mesh peer, so it has to revoke reliably.

With POST/DELETE /mesh/peers gone, "edit the file" is the whole revocation story.
It used to fail silently: deleting the file kept every peer in memory (and the next
`last_seen` write put them back on disk), a file that did not parse kept the old
peers, and the controller rewrote the file on every permitted request, racing the
host's own edit. The registry now only reads the file:

- a missing file is an empty registry;
- a file that is unreadable, not JSON, not an object, or holds any entry that is not
  a valid peer accepts nobody (the whole file is refused, not just the bad entry),
  and says so in the log;
- `last_seen` lives in memory and is never written to the file;
- every check re-stats the file (modification time, size, identity), so an edit
  applies to the next request.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.mesh.models import PeerRecord
from app.mesh.peers import PeerRegistryFile
from tests import test_mesh_peer_registration as base
from tests.test_mesh_peer_registration import OBSERVE, add_legit_peer, send

PEERS_LOGGER = "app.mesh.peers"
mesh = base.mesh  # the same fixture: a server, a legit and an attacker identity, the peers file, a test client
_edits = iter(range(10, 10_000))


def entry(identity, *capabilities: str) -> dict:
    return {
        "node_id": identity.node_id,
        "pubkey_b64": identity.pubkey_b64,
        "grants": [{"capability": capability} for capability in capabilities],
    }


def edit(path: Path, content: str | dict) -> None:
    """Write the file the way a host operator would, with a modification time that is new every time.

    (A clock that ticks every few milliseconds can give two quick edits one timestamp.)
    """
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() + next(_edits)
    os.utime(path, (stamp, stamp))


def warnings_from_peers(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == PEERS_LOGGER and r.levelno >= logging.WARNING]


# ── a missing file is an empty registry ─────────────────────────────────────


def test_a_registry_without_a_file_is_empty(tmp_path: Path) -> None:
    registry = PeerRegistryFile(tmp_path / "peers.json")

    assert registry.all() == []
    assert registry.get_optional("anyone") is None
    with pytest.raises(KeyError):
        registry.get("anyone")


def test_deleting_the_file_revokes_every_peer_at_once(mesh) -> None:
    add_legit_peer(mesh, OBSERVE)
    assert send(mesh, mesh.legit).status_code == 200

    mesh.path.unlink()

    assert send(mesh, mesh.legit).status_code == 403
    assert mesh.peers.all() == []
    assert not mesh.path.exists()  # and nothing wrote the revoked peers back


def test_a_file_that_comes_back_is_read_again(mesh) -> None:
    add_legit_peer(mesh, OBSERVE)
    mesh.path.unlink()
    assert send(mesh, mesh.legit).status_code == 403

    edit(mesh.path, {mesh.legit.node_id: entry(mesh.legit, OBSERVE)})

    assert send(mesh, mesh.legit).status_code == 200


# ── a file that is not valid accepts nobody ─────────────────────────────────

_ID = "a" * 64
NESTING = 100_000
INVALID_FILES = {
    "not json": "{not json",
    "empty": "",
    "null": "null",
    "a list": "[]",
    "a string": '"text"',
    "an entry that is not an object": json.dumps({_ID: ["not", "an", "object"]}),
    "an entry without a key": json.dumps({_ID: {"node_id": _ID}}),
    # json.loads raises RecursionError here, not ValueError; /mesh/receive needs no credential, so
    # a file like this must be refused and logged once, not answered with a traceback per request.
    "nested 100000 deep": "[" * NESTING + "]" * NESTING,
}


@pytest.mark.parametrize("content", INVALID_FILES.values(), ids=INVALID_FILES.keys())
def test_a_file_that_is_not_valid_accepts_nobody_and_says_so(mesh, caplog, content) -> None:
    add_legit_peer(mesh, OBSERVE)
    assert send(mesh, mesh.legit).status_code == 200

    with caplog.at_level(logging.WARNING, logger=PEERS_LOGGER):
        edit(mesh.path, content)
        response = send(mesh, mesh.legit)

    assert response.status_code == 403
    assert mesh.calls == [("browser.observe", {})]  # only the request made before the edit ran
    assert mesh.peers.all() == []
    assert len(warnings_from_peers(caplog)) == 1


def test_one_bad_entry_refuses_the_whole_file(mesh) -> None:
    add_legit_peer(mesh, OBSERVE)
    assert send(mesh, mesh.legit).status_code == 200
    edit(
        mesh.path,
        {mesh.legit.node_id: entry(mesh.legit, OBSERVE), _ID: {"node_id": _ID}},  # the second has no key
    )

    assert send(mesh, mesh.legit).status_code == 403
    assert mesh.peers.all() == []


def test_the_peers_are_accepted_again_once_the_file_is_valid(mesh) -> None:
    add_legit_peer(mesh, OBSERVE)
    edit(mesh.path, "{not json")
    assert send(mesh, mesh.legit).status_code == 403

    edit(mesh.path, {mesh.legit.node_id: entry(mesh.legit, OBSERVE)})

    assert send(mesh, mesh.legit).status_code == 200


def test_an_invalid_file_at_startup_accepts_nobody_and_says_so(tmp_path: Path, caplog) -> None:
    path = tmp_path / "peers.json"
    path.write_text("{oops", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=PEERS_LOGGER):
        registry = PeerRegistryFile(path)

    assert registry.all() == []
    assert len(warnings_from_peers(caplog)) == 1


def test_an_invalid_file_is_logged_once_not_on_every_request(mesh, caplog) -> None:
    add_legit_peer(mesh, OBSERVE)
    edit(mesh.path, "{not json")

    with caplog.at_level(logging.WARNING, logger=PEERS_LOGGER):
        for _ in range(3):
            assert send(mesh, mesh.legit).status_code == 403

    assert len(warnings_from_peers(caplog)) == 1


def test_a_deeply_nested_file_is_logged_once_and_never_raises(mesh, caplog) -> None:
    add_legit_peer(mesh, OBSERVE)
    edit(mesh.path, "[" * NESTING + "]" * NESTING)

    with caplog.at_level(logging.WARNING, logger=PEERS_LOGGER):
        for _ in range(3):
            assert send(mesh, mesh.legit).status_code == 403
            assert mesh.peers.all() == []

    assert len(warnings_from_peers(caplog)) == 1
    assert not [r for r in caplog.records if r.exc_info]  # a refusal, not a traceback


def test_a_file_that_disappears_is_logged(mesh, caplog) -> None:
    add_legit_peer(mesh, OBSERVE)

    with caplog.at_level(logging.WARNING, logger=PEERS_LOGGER):
        mesh.path.unlink()
        mesh.peers.all()
        mesh.peers.all()

    assert len(warnings_from_peers(caplog)) == 1


def test_a_file_that_cannot_be_read_accepts_nobody_and_is_read_again_next_time(mesh, caplog) -> None:
    add_legit_peer(mesh, OBSERVE)
    edit(mesh.path, {mesh.legit.node_id: entry(mesh.legit, OBSERVE)})

    with caplog.at_level(logging.WARNING, logger=PEERS_LOGGER):
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            assert send(mesh, mesh.legit).status_code == 403
        assert len(warnings_from_peers(caplog)) == 1

    # The file did not change, but it was never read: a transient error must not stick.
    assert send(mesh, mesh.legit).status_code == 200


# ── last_seen stays in memory ───────────────────────────────────────────────


def test_a_permitted_request_does_not_rewrite_the_file(mesh) -> None:
    add_legit_peer(mesh, OBSERVE)
    before = (mesh.path.read_bytes(), mesh.path.stat().st_mtime_ns)

    assert send(mesh, mesh.legit).status_code == 200

    assert (mesh.path.read_bytes(), mesh.path.stat().st_mtime_ns) == before
    assert json.loads(before[0])[mesh.legit.node_id]["last_seen"] == 0


def test_a_file_the_host_just_wrote_is_exactly_what_stays_on_disk(mesh) -> None:
    written = json.dumps({mesh.legit.node_id: entry(mesh.legit, OBSERVE)}, indent=1)
    edit(mesh.path, written)

    assert send(mesh, mesh.legit).status_code == 200

    assert mesh.path.read_text(encoding="utf-8") == written


def test_last_seen_is_reported_from_memory(mesh) -> None:
    add_legit_peer(mesh, OBSERVE)
    assert mesh.peers.get(mesh.legit.node_id).last_seen == 0

    assert send(mesh, mesh.legit).status_code == 200

    assert mesh.peers.get(mesh.legit.node_id).last_seen > 0
    assert mesh.peers.get_optional(mesh.legit.node_id).last_seen > 0
    assert [p.last_seen > 0 for p in mesh.peers.all()] == [True]
    assert mesh.client.get("/mesh/peers").json()["peers"][0]["last_seen"] > 0


def test_last_seen_survives_an_edit_but_not_a_removal(mesh) -> None:
    add_legit_peer(mesh, OBSERVE)
    assert send(mesh, mesh.legit).status_code == 200
    seen = mesh.peers.get(mesh.legit.node_id).last_seen

    edit(mesh.path, {mesh.legit.node_id: entry(mesh.legit, OBSERVE), mesh.attacker.node_id: entry(mesh.attacker)})
    assert mesh.peers.get(mesh.legit.node_id).last_seen == seen

    edit(mesh.path, {})
    assert mesh.peers.all() == []
    edit(mesh.path, {mesh.legit.node_id: entry(mesh.legit, OBSERVE)})
    assert mesh.peers.get(mesh.legit.node_id).last_seen == 0


def test_recording_last_seen_for_a_peer_that_is_gone_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "peers.json"
    registry = PeerRegistryFile(path)

    registry.update_last_seen("nobody")

    assert not path.exists()


# ── an edit applies to the next check ───────────────────────────────────────


def test_an_edit_is_seen_even_when_the_modification_time_does_not_move(tmp_path: Path) -> None:
    path = tmp_path / "peers.json"
    peer = PeerRecord(node_id="n1", pubkey_b64="A" * 44)
    path.write_text(json.dumps({"n1": peer.model_dump()}), encoding="utf-8")
    stamp = path.stat().st_mtime_ns
    registry = PeerRegistryFile(path)
    assert [p.node_id for p in registry.all()] == ["n1"]

    path.write_text("{}", encoding="utf-8")
    os.utime(path, ns=(stamp, stamp))

    assert registry.all() == []


def test_a_replacement_file_is_seen_even_with_the_same_size_and_modification_time(tmp_path: Path) -> None:
    path = tmp_path / "peers.json"
    # A fixed added_at: time.time() floats print with a varying number of digits.
    first = json.dumps({"n1": PeerRecord(node_id="n1", pubkey_b64="A" * 44, added_at=1.0).model_dump()})
    second = json.dumps({"n2": PeerRecord(node_id="n2", pubkey_b64="A" * 44, added_at=1.0).model_dump()})
    assert len(first) == len(second)
    path.write_text(first, encoding="utf-8")
    stamp = path.stat().st_mtime_ns
    registry = PeerRegistryFile(path)
    assert registry.get_optional("n1") is not None

    replacement = tmp_path / "peers.json.new"
    replacement.write_text(second, encoding="utf-8")
    os.utime(replacement, ns=(stamp, stamp))
    os.replace(replacement, path)  # the atomic replace docs/mesh.md recommends

    assert registry.get_optional("n1") is None
    assert registry.get_optional("n2") is not None
