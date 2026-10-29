"""Missing evidence is never reported as valid.

`GET /sessions/{id}/witness/verify` answered `valid: true` when the chain file was
gone (even with its anchor still recording receipts), for a session that never
existed, and when the anchor was deleted after the chain was cut short. Someone
with write access to the witness directory could therefore hide a truncation, and
a client that read only `valid` could not tell.

Every test here goes through the route a client calls, or the MCP tool, on a real
manager with signing on.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import app.main as main_module
from app.browser_manager import BrowserManager
from app.models import McpToolCallRequest, OperatorIdentity, SessionRecord
from app.routes._utils import require_session_access
from app.routes.session_diagnostics import create_session_diagnostics_router
from app.session_ownership import acting_as
from app.tool_gateway import McpToolGateway
from app.witness import WitnessRecorder
from app.witness_anchor import anchor_path, read_anchor
from tests.test_browser_manager_create_session import _settings
from tests.test_witness_signing import requires_verifier

SCOPE = "sess-1"
VERIFIER = Path(__file__).resolve().parents[2] / "scripts" / "verify_witness_bundle.py"


def _session_record(session_id: str = SCOPE) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        name=session_id,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        status="closed",
        live=False,
        current_url="https://example.com",
        title="t",
        artifact_dir="/tmp",
        takeover_url="http://127.0.0.1:6080",
        remote_access={},
    )


@pytest.fixture
async def manager(tmp_path: Path) -> BrowserManager:
    manager = BrowserManager(_settings(tmp_path))
    await manager.session_store.startup()
    await manager.session_store.upsert(_session_record())
    return manager


@pytest.fixture
def client(manager: BrowserManager):
    app = FastAPI()
    app.add_exception_handler(KeyError, main_module.handle_key_not_found)
    app.include_router(create_session_diagnostics_router(manager=manager, settings=manager.settings))
    with TestClient(app) as test_client:
        yield test_client


async def _record(manager: BrowserManager, count: int, scope: str = SCOPE) -> None:
    for index in range(count):
        await manager.witness.record(
            scope,
            profile="normal",
            event_type="action",
            status="ok",
            action=f"click-{index}",
            action_class="write",
            session_id=scope,
            operator=OperatorIdentity(id="op-1"),
        )


def _verify(client: TestClient, scope: str = SCOPE):
    return client.get(f"/sessions/{scope}/witness/verify")


def _truncate_to(path: Path, keep: int) -> None:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    path.write_text("\n".join(lines[:keep]) + "\n", encoding="utf-8")


async def test_an_intact_chain_is_valid(manager, client) -> None:
    await _record(manager, 3)

    body = _verify(client).json()

    assert body["valid"] is True
    assert body["anchor"]["status"] == "ok"


async def test_truncation_with_the_anchor_present_is_caught(manager, client) -> None:
    await _record(manager, 4)
    _truncate_to(manager.witness._path(SCOPE), 2)

    body = _verify(client).json()

    assert body["valid"] is False
    assert body["anchor"]["status"] == "truncated"


async def test_a_deleted_chain_whose_anchor_remains_is_not_valid(manager, client) -> None:
    await _record(manager, 3)
    path = manager.witness._path(SCOPE)
    path.unlink()
    assert anchor_path(path).exists()

    response = _verify(client)
    body = response.json()

    assert response.status_code == 200
    assert body["valid"] is False, body
    assert body["anchor"]["expected_receipt_count"] == 3
    assert "3" in body["reason"]


async def test_a_chain_cut_short_with_its_anchor_deleted_is_not_valid(manager, client) -> None:
    await _record(manager, 4)
    path = manager.witness._path(SCOPE)
    _truncate_to(path, 2)
    anchor_path(path).unlink()

    body = _verify(client).json()

    assert body["receipt_count"] == 2
    assert body["valid"] is False, body
    assert body["anchor"]["status"] != "ok"
    assert "anchor" in body["reason"]


async def test_a_session_with_both_files_deleted_is_not_valid(manager, client) -> None:
    await _record(manager, 3)
    path = manager.witness._path(SCOPE)
    path.unlink()
    anchor_path(path).unlink()

    response = _verify(client)

    assert response.status_code == 200
    assert response.json()["valid"] is False


async def test_a_known_session_with_no_receipts_is_not_valid(client) -> None:
    body = _verify(client).json()

    assert body["valid"] is False
    assert body["receipt_count"] == 0
    assert "no receipts" in body["reason"]


def test_an_unknown_session_is_a_404(client) -> None:
    response = _verify(client, "never-existed")

    assert response.status_code == 404
    assert "valid" not in response.json()


async def test_a_chain_that_is_not_a_session_is_still_verifiable(manager, client) -> None:
    """The auth-profile receipts are kept under their own scope, with no session record."""
    await _record(manager, 2, scope="auth-profiles")

    response = _verify(client, "auth-profiles")

    assert response.status_code == 200
    assert response.json()["valid"] is True


@pytest.mark.parametrize(
    ("named", "operator", "status"),
    [
        (True, OperatorIdentity(id="alice", source="token"), 404),
        (True, OperatorIdentity(id="system", source="system"), 404),
        (False, OperatorIdentity(id="alice", source="header"), 200),
    ],
    ids=["named-operator", "named-system", "control-unnamed"],
)
async def test_under_named_credentials_the_auth_profile_chain_is_refused_like_any_id_with_no_record(
    manager, named: bool, operator: OperatorIdentity, status: int
) -> None:
    """COMBINE-01, decided: kept. The app-wide dependency refuses an id with no live session and no record under
    named credentials before the witness routes run, the auth-profile chain's among them (which also keeps one
    operator from another's profile-export receipts). The bare router above does not run that dependency."""
    if named:
        manager.settings.api_bearer_tokens = "alice:token-a,bob:token-b"
    await _record(manager, 2, scope="auth-profiles")
    app = FastAPI(dependencies=[Depends(require_session_access)])
    app.state.browser_manager = manager
    app.add_exception_handler(KeyError, main_module.handle_key_not_found)
    app.include_router(create_session_diagnostics_router(manager=manager, settings=manager.settings))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        with acting_as(operator):
            responses = [await client.get(f"/sessions/auth-profiles/witness/{leaf}") for leaf in ("verify", "bundle")]

    assert [response.status_code for response in responses] == [status, status], [r.text for r in responses]


@pytest.mark.parametrize(
    "build",
    [
        lambda head: "{not json",
        lambda head: "[]",
        lambda head: "{}",
        lambda head: json.dumps({"head_hash": head}),
        lambda head: json.dumps({"head_hash": head, "receipt_count": "3"}),
        lambda head: json.dumps({"receipt_count": 3}),
    ],
    ids=["garbage", "not-an-object", "empty-object", "no-count", "count-not-a-number", "no-head"],
)
async def test_a_corrupt_anchor_is_not_valid(manager, client, build) -> None:
    await _record(manager, 3)
    path = anchor_path(manager.witness._path(SCOPE))
    path.write_text(build(read_anchor(manager.witness._path(SCOPE))["head_hash"]), encoding="utf-8")

    body = _verify(client).json()

    assert body["valid"] is False, body
    assert body["anchor"]["status"] != "ok"
    assert "anchor" in body["reason"]


async def test_an_unreadable_anchor_is_not_valid(manager, client) -> None:
    await _record(manager, 3)
    path = anchor_path(manager.witness._path(SCOPE))
    path.unlink()
    path.mkdir()  # reading a directory raises OSError

    body = _verify(client).json()

    assert body["valid"] is False, body


async def test_the_mcp_tool_reports_the_same_verdicts(manager) -> None:
    gateway = McpToolGateway(manager=manager, orchestrator=MagicMock(), job_queue=MagicMock(), tool_profile="full")
    await _record(manager, 3)
    manager.witness._path(SCOPE).unlink()

    missing = await gateway.call_tool(
        McpToolCallRequest(name="browser.verify_witness", arguments={"session_id": SCOPE})
    )
    unknown = await gateway.call_tool(
        McpToolCallRequest(name="browser.verify_witness", arguments={"session_id": "never-existed"})
    )

    assert missing.isError is False
    assert missing.structuredContent["valid"] is False
    assert unknown.isError is True


async def test_an_export_of_a_deleted_chain_does_not_claim_a_valid_chain(manager) -> None:
    await _record(manager, 3)
    manager.witness._path(SCOPE).unlink()

    bundle = await manager.export_witness_bundle(SCOPE)

    assert bundle["receipt_count"] == 0
    assert bundle["chain_valid_at_export"] is False


@requires_verifier
async def test_the_standalone_verifier_rejects_a_bundle_with_no_receipts(manager, tmp_path: Path) -> None:
    await _record(manager, 3)
    manager.witness._path(SCOPE).unlink()
    bundle = await manager.export_witness_bundle(SCOPE)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(VERIFIER), str(path)], capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 1, result.stdout
    assert "no receipts" in result.stdout


# Chains written before the anchor existed have none. The first start anchors them
# as they are, so upgrading does not turn every old chain invalid.


async def test_the_first_start_anchors_a_legacy_chain_as_it_is(manager, client) -> None:
    await _record(manager, 3)
    path = manager.witness._path(SCOPE)
    head = read_anchor(path)["head_hash"]
    anchor_path(path).unlink()  # as a chain from before anchors existed: no anchor, no marker in the root
    assert not (manager.witness.root / ".anchors-migrated").exists()
    assert _verify(client).json()["valid"] is False

    await manager.witness.startup()

    assert read_anchor(path) == {"head_hash": head, "receipt_count": 3}
    assert _verify(client).json()["valid"] is True


async def test_starting_again_leaves_an_existing_anchor_alone(manager, client) -> None:
    await _record(manager, 4)
    path = manager.witness._path(SCOPE)
    _truncate_to(path, 2)

    await manager.witness.startup()

    assert read_anchor(path)["receipt_count"] == 4
    assert _verify(client).json()["anchor"]["status"] == "truncated"


async def test_a_legacy_chain_that_cannot_be_read_does_not_stop_the_start(manager, client) -> None:
    await _record(manager, 1)
    await _record(manager, 2, scope="legacy-good")
    bad = manager.witness._path("legacy-bad")
    bad.write_text("this is not a receipt\n", encoding="utf-8")
    anchor_path(manager.witness._path("legacy-good")).unlink()
    assert not anchor_path(manager.witness._path("legacy-good")).exists()

    await manager.witness.startup()

    assert not anchor_path(bad).exists()
    assert read_anchor(manager.witness._path("legacy-good"))["receipt_count"] == 2
    assert (manager.witness.root / ".anchors-migrated").exists(), "one unreadable chain does not repeat the pass"


async def test_a_chain_appended_after_the_start_keeps_its_anchor_in_step(manager, client) -> None:
    await _record(manager, 2)
    anchor_path(manager.witness._path(SCOPE)).unlink()
    await manager.witness.startup()

    await _record(manager, 1)

    body = _verify(client).json()
    assert body["valid"] is True
    assert body["anchor"]["expected_receipt_count"] == 3


# ── An anchor is never created or advanced over a chain it does not describe ──


def _restart(manager: BrowserManager) -> None:
    """A new process: a fresh recorder over the same root and key."""
    manager.witness = WitnessRecorder(manager.witness.root, signer=manager.witness.signer)


async def _after_the_migration(manager: BrowserManager) -> None:
    await manager.witness.startup()


async def test_deleting_an_anchor_after_the_migration_stays_anchor_missing_across_a_restart(manager, client) -> None:
    await _after_the_migration(manager)
    await _record(manager, 4)
    path = manager.witness._path(SCOPE)
    _truncate_to(path, 2)
    anchor_path(path).unlink()
    assert _verify(client).json()["anchor"]["status"] == "anchor_missing"

    _restart(manager)
    await manager.witness.startup()

    body = _verify(client).json()
    assert body["valid"] is False, body
    assert body["anchor"]["status"] == "anchor_missing"
    assert not anchor_path(path).exists()


async def test_the_migration_runs_once_and_leaves_a_marker(manager) -> None:
    await manager.witness.startup()

    assert (manager.witness.root / ".anchors-migrated").exists()


async def test_an_append_with_a_truncated_chain_keeps_it_flagged(manager, client) -> None:
    await _after_the_migration(manager)
    await _record(manager, 4)
    path = manager.witness._path(SCOPE)
    _truncate_to(path, 2)
    _restart(manager)
    await manager.witness.startup()

    await _record(manager, 1)

    body = _verify(client).json()
    assert body["valid"] is False, body
    assert body["anchor"]["status"] == "truncated"
    assert body["anchor"]["expected_receipt_count"] == 4
    assert body["receipt_count"] == 3, "the receipt is recorded all the same"
    assert len(await manager.witness.list(SCOPE, limit=10)) == 3


async def test_an_append_after_the_chain_was_deleted_keeps_the_anchor(manager, client) -> None:
    await _after_the_migration(manager)
    await _record(manager, 3)
    path = manager.witness._path(SCOPE)
    path.unlink()

    await _record(manager, 1)

    body = _verify(client).json()
    assert body["valid"] is False, body
    assert body["anchor"]["expected_receipt_count"] == 3
    assert body["receipt_count"] == 1


async def test_an_append_to_a_chain_with_no_anchor_does_not_create_one(manager, client) -> None:
    await _after_the_migration(manager)
    await _record(manager, 2)
    path = manager.witness._path(SCOPE)
    anchor_path(path).unlink()

    await _record(manager, 1)

    assert not anchor_path(path).exists()
    body = _verify(client).json()
    assert body["anchor"]["status"] == "anchor_missing"
    assert body["receipt_count"] == 3


async def test_an_append_after_a_crash_between_the_receipt_and_its_anchor_catches_up(manager, client) -> None:
    await _after_the_migration(manager)
    await _record(manager, 3)
    path = manager.witness._path(SCOPE)
    behind = anchor_path(path).read_text(encoding="utf-8")
    await _record(manager, 1)
    anchor_path(path).write_text(behind, encoding="utf-8")  # the 4th receipt landed, its anchor did not

    await _record(manager, 1)

    body = _verify(client).json()
    assert body["valid"] is True, body
    assert body["anchor"]["expected_receipt_count"] == 5


async def test_an_append_leaves_a_malformed_anchor_for_verify_to_report(manager, client) -> None:
    await _after_the_migration(manager)
    await _record(manager, 2)
    anchor_path(manager.witness._path(SCOPE)).write_bytes(b"\xff\xfe{")

    await _record(manager, 1)

    body = _verify(client).json()
    assert body["valid"] is False
    assert body["anchor"]["status"] == "anchor_unreadable"
    assert body["receipt_count"] == 3


# ── One rule for a final record that lacks its newline ──


@pytest.mark.parametrize("tail", [b"", b"\r"], ids=["no-newline", "dangling-cr"])
async def test_a_complete_final_record_without_its_newline_is_a_record(manager, client, tail: bytes) -> None:
    await _after_the_migration(manager)
    await _record(manager, 4)
    path = manager.witness._path(SCOPE)
    path.write_bytes(path.read_bytes().rstrip(b"\r\n") + tail)

    body = _verify(client).json()
    assert body["valid"] is True, body
    assert body["receipt_count"] == 4
    assert len(await manager.witness.list(SCOPE, limit=10)) == 4
    assert (await manager.export_witness_bundle(SCOPE))["receipt_count"] == 4

    _restart(manager)
    await manager.witness.startup()
    await _record(manager, 1)

    body = _verify(client).json()
    assert body["valid"] is True, body
    assert body["receipt_count"] == 5
    assert not list(manager.witness.root.glob("*.torn-*")), "nothing was set aside"


async def test_an_incomplete_final_record_is_still_set_aside(manager, client) -> None:
    await _after_the_migration(manager)
    await _record(manager, 2)
    path = manager.witness._path(SCOPE)
    with path.open("ab") as handle:
        handle.write(b'{"receipt_id": "x", "trunc')

    await _record(manager, 1)

    assert len(list(manager.witness.root.glob("*.torn-*"))) == 1
    assert _verify(client).json()["receipt_count"] == 3


# ── Anchors are parsed strictly and compared exactly ──


async def test_an_anchor_that_is_not_utf8_is_unreadable_not_an_error(manager, client) -> None:
    await _record(manager, 2)
    anchor_path(manager.witness._path(SCOPE)).write_bytes(b"\xff\xfe{")

    response = _verify(client)

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["anchor"]["status"] == "anchor_unreadable"


@pytest.mark.parametrize("count", [True, -1, 0, "4", 4.0], ids=["bool", "negative", "zero", "string", "float"])
async def test_an_anchor_count_that_is_not_a_positive_integer_is_unreadable(manager, client, count) -> None:
    await _record(manager, 4)
    path = manager.witness._path(SCOPE)
    anchor = read_anchor(path)
    anchor_path(path).write_text(json.dumps({**anchor, "receipt_count": count}), encoding="utf-8")

    body = _verify(client).json()

    assert body["valid"] is False, body
    assert body["anchor"]["status"] == "anchor_unreadable"


@pytest.mark.parametrize("head", ["", None, 7], ids=["empty", "null", "number"])
async def test_an_anchor_head_that_is_not_a_non_empty_string_is_unreadable(manager, client, head) -> None:
    await _record(manager, 2)
    path = manager.witness._path(SCOPE)
    anchor_path(path).write_text(json.dumps({"head_hash": head, "receipt_count": 2}), encoding="utf-8")

    body = _verify(client).json()

    assert body["valid"] is False
    assert body["anchor"]["status"] == "anchor_unreadable"


async def test_the_right_head_with_a_smaller_count_has_diverged(manager, client) -> None:
    await _record(manager, 4)
    path = manager.witness._path(SCOPE)
    anchor = read_anchor(path)
    anchor_path(path).write_text(json.dumps({**anchor, "receipt_count": 2}), encoding="utf-8")

    body = _verify(client).json()

    assert body["valid"] is False
    assert body["anchor"]["status"] == "diverged"


# ── The signatures check never raises on bad evidence ──


async def test_a_partial_final_record_fails_the_route_with_a_verdict_not_an_error(manager, client) -> None:
    await _record(manager, 2)
    with manager.witness._path(SCOPE).open("ab") as handle:
        handle.write(b'{"receipt_id": "x", "trunc')

    response = _verify(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is False
    assert body["signatures"]["valid"] is False
    assert "not parseable" in body["signatures"]["reason"]


async def test_a_chain_that_is_not_utf8_fails_with_a_verdict(manager, client) -> None:
    await _record(manager, 2)
    path = manager.witness._path(SCOPE)
    path.write_bytes(path.read_bytes() + b"\xff\xfe\n")

    response = _verify(client)

    assert response.status_code == 200, response.text
    assert response.json()["valid"] is False


# ── Blank means ASCII whitespace, everywhere ──


async def test_ascii_blank_lines_are_ignored_by_every_reader_and_by_an_append(manager, client) -> None:
    await _after_the_migration(manager)
    await _record(manager, 2)
    path = manager.witness._path(SCOPE)
    path.write_bytes(path.read_bytes() + b" \t\x0b\x0c\r\n\n")

    await _record(manager, 1)

    body = _verify(client).json()
    assert body["valid"] is True, body
    assert body["receipt_count"] == 3
    assert len(await manager.witness.list(SCOPE, limit=10)) == 3


async def test_a_unicode_whitespace_line_is_a_record_that_fails_not_a_blank(manager, client) -> None:
    await _record(manager, 2)
    path = manager.witness._path(SCOPE)
    path.write_bytes(path.read_bytes() + "\u0085\n".encode())

    body = _verify(client).json()

    assert body["valid"] is False
    assert "not parseable" in body["reason"]


# ── Export answers an unknown id like verify ──


def test_export_for_an_unknown_session_is_a_404(client) -> None:
    response = client.get("/sessions/never-existed/witness/bundle")

    assert response.status_code == 404


async def test_export_for_a_scope_with_a_chain_and_no_session_still_works(manager, client) -> None:
    await _record(manager, 2, scope="auth-profiles")

    response = client.get("/sessions/auth-profiles/witness/bundle")

    assert response.status_code == 200
    assert response.json()["receipt_count"] == 2


async def test_an_append_does_not_move_the_anchor_to_a_longer_chain_that_is_not_the_anchored_one(
    manager, client
) -> None:
    await _after_the_migration(manager)
    await _record(manager, 3)
    path = manager.witness._path(SCOPE)
    anchored = anchor_path(path).read_text(encoding="utf-8")
    await _record(manager, 4, scope="elsewhere")  # a different, internally consistent chain, longer than the anchor
    path.write_bytes(manager.witness._path("elsewhere").read_bytes())

    await _record(manager, 1)

    assert anchor_path(path).read_text(encoding="utf-8") == anchored
    body = _verify(client).json()
    assert body["valid"] is False
    assert body["anchor"]["status"] == "diverged"


# ── Round 4: the migration repairs a torn tail; readers agree on what a record is ──


async def test_the_migration_sets_a_torn_tail_aside_and_anchors_the_rest(manager, client) -> None:
    await _record(manager, 2)
    path = manager.witness._path(SCOPE)
    anchor_path(path).unlink()  # a legacy chain, then a crash mid-append
    with path.open("ab") as handle:
        handle.write(b'{"receipt_id": "x", "trunc')

    await manager.witness.startup()

    assert len(list(manager.witness.root.glob("*.torn-*"))) == 1
    assert read_anchor(path)["receipt_count"] == 2
    body = _verify(client).json()
    assert body["valid"] is True, body
    assert body["receipt_count"] == 2


async def test_listing_does_not_hide_a_terminated_bad_record_followed_by_blanks(manager) -> None:
    await _record(manager, 2)
    path = manager.witness._path(SCOPE)
    path.write_bytes(path.read_bytes() + b"{}\n ")

    with pytest.raises(ValueError):  # the way export and the other readers treat it
        await manager.witness.list(SCOPE, limit=10)
    with pytest.raises(ValueError):
        await manager.witness.export_bundle(SCOPE)


async def test_listing_still_drops_an_unterminated_fragment_that_is_not_a_receipt(manager) -> None:
    await _record(manager, 2)
    path = manager.witness._path(SCOPE)
    path.write_bytes(path.read_bytes() + b"{}")

    assert len(await manager.witness.list(SCOPE, limit=10)) == 2


async def test_a_receipt_with_a_null_chain_hash_is_a_record_for_list_export_and_append(manager) -> None:
    await _after_the_migration(manager)
    await _record(manager, 1)
    path = manager.witness._path(SCOPE)
    receipt = json.loads(path.read_text(encoding="utf-8").strip())
    receipt["chain_hash"] = None
    path.write_bytes(json.dumps(receipt).encode("utf-8"))  # no newline

    assert len(await manager.witness.list(SCOPE, limit=10)) == 1
    assert (await manager.export_witness_bundle(SCOPE))["receipt_count"] == 1

    await _record(manager, 1)

    assert not list(manager.witness.root.glob("*.torn-*")), "a parsed receipt is never set aside"
    assert len(await manager.witness.list(SCOPE, limit=10)) == 2


async def test_bytes_that_are_not_utf8_do_not_verify_even_where_replacement_would_restore_the_text(
    manager, client
) -> None:
    await _after_the_migration(manager)
    await manager.witness.record(
        SCOPE,
        profile="normal",
        event_type="action",
        status="ok",
        action="click",
        action_class="write",
        session_id=SCOPE,
        operator=OperatorIdentity(id="op-1"),
        metadata={"title": "caf�"},
    )
    path = manager.witness._path(SCOPE)
    data = path.read_bytes()
    assert data.count("�".encode()) == 1
    path.write_bytes(data.replace("�".encode(), b"\xff"))

    body = _verify(client).json()

    assert body["valid"] is False, body
    assert "not parseable" in body["reason"]
    assert body["signatures"]["valid"] is False
    with pytest.raises(ValueError):
        await manager.witness.list(SCOPE, limit=10)
    with pytest.raises(ValueError):
        await manager.witness.export_bundle(SCOPE)


async def test_a_chain_missing_a_middle_receipt_still_fails_after_an_append(manager, client) -> None:
    """The append keeps the O(1) head-match path; the hash walk is what catches the gap."""
    await _after_the_migration(manager)
    await _record(manager, 4)
    path = manager.witness._path(SCOPE)
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    path.write_text("\n".join([lines[0], lines[2], lines[3]]) + "\n", encoding="utf-8")

    await _record(manager, 1)

    body = _verify(client).json()
    assert body["valid"] is False, body
    assert body["first_invalid_index"] == 1


async def test_the_documented_recovery_deleting_the_marker_and_restarting_anchors_the_chain(manager, client) -> None:
    await _after_the_migration(manager)
    await _record(manager, 2)
    path = manager.witness._path(SCOPE)
    anchor_path(path).unlink()  # as after a crash between the first receipt and its anchor
    assert _verify(client).json()["anchor"]["status"] == "anchor_missing"

    (manager.witness.root / ".anchors-migrated").unlink()
    _restart(manager)
    await manager.witness.startup()

    assert _verify(client).json()["valid"] is True
