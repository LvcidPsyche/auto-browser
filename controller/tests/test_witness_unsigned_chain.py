"""A witness chain with every signature stripped must not verify as signed.

`verify_signatures` rejected an unsigned receipt that FOLLOWED a signed one, but a
chain where no receipt was signed passed (`valid: True`). Someone who can write the
witness directory can edit a receipt, drop every signature, recompute the hashes and
the anchor, and the controller then answered `signatures.valid: true`. The standalone
`scripts/verify_witness_bundle.py` already refuses such a chain; these tests hold the
in-controller check to the same rule, through the path the REST route uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.browser_manager import BrowserManager
from app.models import OperatorIdentity
from app.routes.session_diagnostics import create_session_diagnostics_router
from app.witness import WitnessReceipt, WitnessRecorder
from app.witness_anchor import write_anchor
from app.witness_signing import WitnessSigner
from tests.test_browser_manager_create_session import _settings

SCOPE = "sess-1"
NO_SIGNED_RECEIPT = "no receipt is signed"


def _kwargs(action: str) -> dict:
    return {
        "profile": "normal",
        "event_type": "action",
        "status": "ok",
        "action": action,
        "action_class": "write",
        "session_id": SCOPE,
        "operator": OperatorIdentity(id="op-1"),
    }


async def _record(recorder: WitnessRecorder, *actions: str) -> None:
    for action in actions:
        await recorder.record(SCOPE, **_kwargs(action))


def _strip_and_rehash(recorder: WitnessRecorder, *, forge_action: str | None = None) -> None:
    """What an attacker with write access to the witness directory does: optionally edit
    a receipt, drop every signature, re-link and re-hash the chain, and rewrite the anchor.
    Nothing here needs the signing key."""
    path = recorder._path(SCOPE)
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    previous: str | None = None
    out: list[str] = []
    for index, line in enumerate(lines):
        receipt = WitnessReceipt.model_validate_json(line)
        if forge_action is not None and index == 1:
            receipt.action = forge_action
        receipt.chain_signature = None
        receipt.signing_key_id = None
        receipt.chain_prev_hash = previous
        receipt.chain_hash = WitnessRecorder._compute_hash(receipt)
        previous = receipt.chain_hash
        out.append(receipt.model_dump_json())
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    write_anchor(path, head_hash=previous or "", receipt_count=len(out))


@pytest.fixture
def manager(tmp_path: Path) -> BrowserManager:
    # BrowserManager builds its own signer under <witness_root>/keys, as in production.
    return BrowserManager(_settings(tmp_path))


@pytest.fixture
def client(manager: BrowserManager):
    app = FastAPI()
    app.include_router(create_session_diagnostics_router(manager=manager, settings=manager.settings))
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.asyncio
async def test_a_chain_with_every_signature_stripped_fails_through_the_manager(manager: BrowserManager) -> None:
    assert manager.witness.signer is not None, "signing is on by default; the forgery targets that default"
    await _record(manager.witness, "navigate", "click", "type")
    genuine = await manager.verify_witness_chain(SCOPE)
    assert genuine["signatures"]["valid"] is True

    _strip_and_rehash(manager.witness, forge_action="FORGED_ACTION")

    chain = await manager.verify_witness_chain(SCOPE)
    # The forgery is internally consistent, which is why the hash walk cannot see it.
    assert chain["valid"] is True
    assert chain["signed_count"] == 0
    signatures = chain["signatures"]
    assert signatures["valid"] is False
    assert NO_SIGNED_RECEIPT in signatures["reason"]
    assert (signatures["checked"], signatures["signed"], signatures["unsigned"]) == (3, 0, 3)
    assert signatures["public_key_b64"] == manager.witness.signer.public_key_b64


@pytest.mark.asyncio
async def test_a_chain_with_every_signature_stripped_fails_on_the_rest_route(
    manager: BrowserManager, client: TestClient
) -> None:
    await _record(manager.witness, "navigate", "click")
    _strip_and_rehash(manager.witness, forge_action="FORGED_ACTION")

    response = client.get(f"/sessions/{SCOPE}/witness/verify")

    assert response.status_code == 200, response.text
    signatures = response.json()["signatures"]
    assert signatures["valid"] is False
    assert NO_SIGNED_RECEIPT in signatures["reason"]


@pytest.mark.asyncio
async def test_a_supplied_public_key_is_held_to_the_same_rule(manager: BrowserManager) -> None:
    await _record(manager.witness, "navigate", "click")
    _strip_and_rehash(manager.witness)
    key = manager.witness.signer.public_key_b64

    result = await manager.witness.verify_signatures(SCOPE, public_key_b64=key)

    assert result["valid"] is False
    assert NO_SIGNED_RECEIPT in result["reason"]


@pytest.mark.asyncio
async def test_a_genuine_signed_chain_passes(manager: BrowserManager, client: TestClient) -> None:
    await _record(manager.witness, "navigate", "click", "type")

    signatures = client.get(f"/sessions/{SCOPE}/witness/verify").json()["signatures"]

    assert signatures["valid"] is True
    assert (signatures["checked"], signatures["signed"], signatures["unsigned"]) == (3, 3, 0)
    assert signatures["reason"] is None


@pytest.mark.asyncio
async def test_receipts_that_predate_signing_still_pass_when_a_signed_one_follows(tmp_path: Path) -> None:
    legacy = WitnessRecorder(tmp_path / "witness")
    await _record(legacy, "before-signing")
    signed = WitnessRecorder(tmp_path / "witness", signer=WitnessSigner(tmp_path / "keys"))
    await _record(signed, "navigate", "click")

    result = await signed.verify_signatures(SCOPE)

    assert result["valid"] is True
    assert (result["signed"], result["unsigned"]) == (2, 1)
    assert result["reason"] is None


@pytest.mark.asyncio
async def test_a_signature_removed_after_signed_receipts_still_fails_with_its_own_reason(
    manager: BrowserManager,
) -> None:
    await _record(manager.witness, "navigate", "click", "type")
    path = manager.witness._path(SCOPE)
    lines = path.read_text(encoding="utf-8").splitlines()
    last = WitnessReceipt.model_validate_json(lines[-1])
    last.chain_signature = None
    last.signing_key_id = None
    path.write_text("\n".join([*lines[:-1], last.model_dump_json()]) + "\n", encoding="utf-8")

    signatures = (await manager.verify_witness_chain(SCOPE))["signatures"]

    assert signatures["valid"] is False
    assert signatures["first_invalid_index"] == 2
    assert "a signature was removed" in signatures["reason"]
    assert NO_SIGNED_RECEIPT not in signatures["reason"]


@pytest.mark.asyncio
async def test_an_empty_chain_passes(manager: BrowserManager, tmp_path: Path) -> None:
    never_written = await manager.verify_witness_chain("never-recorded")
    assert never_written["signatures"]["valid"] is True
    assert never_written["signatures"]["checked"] == 0

    empty = manager.witness._path("empty-file")
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("\n", encoding="utf-8")
    result = await manager.witness.verify_signatures("empty-file")
    assert result["valid"] is True
    assert (result["checked"], result["reason"]) == (0, None)
