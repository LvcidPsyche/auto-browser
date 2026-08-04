"""Witness receipts must be attributable, not merely self-consistent.

Before signing, the chain was SHA-256 linked and nothing else: anyone holding
the JSONL could edit a receipt, recompute every downstream hash, and produce a
chain that `verify()` passed. The receipts proved nothing to anyone outside the
box that wrote them.

These tests pin the properties that make a bundle evidence:
  * a tampered receipt fails the hash chain
  * a *consistently re-hashed* tampered chain — the forgery the old design could
    not detect — still fails on signatures
  * an exported bundle verifies with the standalone script, which imports
    nothing from this project
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.witness import WitnessRecorder
from app.witness_signing import WitnessSigner, verify_signature

VERIFIER = Path(__file__).resolve().parents[2] / "scripts" / "verify_witness_bundle.py"

# The controller image COPYs only app/ and tests/, so scripts/ is absent inside
# the container (the controller-tests CI job). The verifier is a tool for bundle
# *recipients*, not for the runtime, so it does not belong in the image — and
# both host-tests jobs run this from a real checkout, so the round-trip stays
# covered in CI.
requires_verifier = pytest.mark.skipif(
    not VERIFIER.is_file(),
    reason="scripts/verify_witness_bundle.py is not shipped inside the controller image",
)


def _receipt_kwargs(action: str = "click"):
    from app.models import OperatorIdentity

    return {
        "profile": "normal",
        "event_type": "action",
        "status": "ok",
        "action": action,
        "action_class": "write",
        "session_id": "sess-1",
        "operator": OperatorIdentity(id="op-1"),
    }


@pytest.fixture
def signed_recorder(tmp_path: Path) -> WitnessRecorder:
    signer = WitnessSigner(tmp_path / "keys")
    recorder = WitnessRecorder(tmp_path / "witness", signer=signer)
    return recorder


@pytest.mark.asyncio
async def test_receipts_are_signed_and_verify(signed_recorder: WitnessRecorder) -> None:
    await signed_recorder.startup()
    receipt = await signed_recorder.record("sess-1", **_receipt_kwargs())

    assert receipt.chain_signature, "receipt must carry a signature"
    assert receipt.signing_key_id == signed_recorder.signer.key_id
    assert verify_signature(
        public_key_b64=signed_recorder.signer.public_key_b64,
        chain_hash=receipt.chain_hash,
        signature_b64=receipt.chain_signature,
    )

    report = await signed_recorder.verify_signatures("sess-1")
    assert report["valid"] is True
    assert report["signed"] == 1
    assert report["unsigned"] == 0


@pytest.mark.asyncio
async def test_a_rehashed_forgery_is_caught_by_signatures(signed_recorder: WitnessRecorder) -> None:
    """The attack the hash chain alone could never detect.

    Edit a receipt, then recompute every chain hash so the chain is internally
    consistent again. verify() passes — it always would have. Only the
    signatures reveal that the holder of the key never attested to this.
    """
    await signed_recorder.startup()
    for i in range(3):
        await signed_recorder.record("sess-1", **_receipt_kwargs(f"click-{i}"))

    path = signed_recorder._path("sess-1")
    receipts = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # Tamper, then re-link the chain so every hash is self-consistent.
    receipts[1]["action"] = "transfer-funds"
    previous = None
    for item in receipts:
        item["chain_prev_hash"] = previous
        payload = {k: v for k, v in item.items() if k not in ("chain_hash", "chain_signature", "signing_key_id")}
        import hashlib

        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        item["chain_hash"] = hashlib.sha256(f"{previous or ''}:{canonical}".encode("utf-8")).hexdigest()
        previous = item["chain_hash"]
    path.write_text("\n".join(json.dumps(r) for r in receipts) + "\n", encoding="utf-8")

    chain = await signed_recorder.verify("sess-1")
    assert chain["valid"] is True, "a re-hashed forgery is internally consistent — this is the gap"

    signatures = await signed_recorder.verify_signatures("sess-1")
    assert signatures["valid"] is False
    # Index 1, not 0: receipt 0 was not touched, so it re-hashes identically and
    # its original signature still verifies. The first failure lands precisely on
    # the receipt that was altered, which is what an investigator needs.
    assert signatures["first_invalid_index"] == 1
    assert signatures["first_invalid_receipt_id"] is not None
    assert "signature does not verify" in signatures["reason"]


@pytest.mark.asyncio
async def test_edited_receipt_without_rehash_fails_the_chain(signed_recorder: WitnessRecorder) -> None:
    """Guard the guard: the cheap tamper is still caught by the cheap check."""
    await signed_recorder.startup()
    await signed_recorder.record("sess-1", **_receipt_kwargs())

    path = signed_recorder._path("sess-1")
    receipt = json.loads(path.read_text(encoding="utf-8").strip())
    receipt["action"] = "transfer-funds"
    path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

    assert (await signed_recorder.verify("sess-1"))["valid"] is False


@pytest.mark.asyncio
async def test_unsigned_chains_are_reported_not_silently_accepted(tmp_path: Path) -> None:
    """An unsigned chain must never look like a verified one."""
    recorder = WitnessRecorder(tmp_path / "witness")  # no signer
    await recorder.startup()
    await recorder.record("sess-1", **_receipt_kwargs())

    chain = await recorder.verify("sess-1")
    assert chain["valid"] is True, "hash chain is still internally consistent"
    assert chain["signed_count"] == 0
    assert chain["unsigned_count"] == 1

    signatures = await recorder.verify_signatures("sess-1")
    assert signatures["valid"] is False
    assert "no public key" in signatures["reason"]


@pytest.mark.asyncio
async def test_pre_signing_receipts_still_verify(tmp_path: Path) -> None:
    """Adding signature fields must not invalidate chains written before them.

    The fields are excluded from chain_payload, so the hash input is byte
    identical to what an older build produced.
    """
    recorder = WitnessRecorder(tmp_path / "witness")
    await recorder.startup()
    await recorder.record("sess-1", **_receipt_kwargs())

    path = recorder._path("sess-1")
    legacy = json.loads(path.read_text(encoding="utf-8").strip())
    legacy.pop("chain_signature", None)
    legacy.pop("signing_key_id", None)
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    assert (await recorder.verify("sess-1"))["valid"] is True


@requires_verifier
@pytest.mark.asyncio
async def test_exported_bundle_verifies_with_the_standalone_script(
    signed_recorder: WitnessRecorder, tmp_path: Path
) -> None:
    """The bundle must be checkable by someone who does not run auto-browser."""
    await signed_recorder.startup()
    for i in range(3):
        await signed_recorder.record("sess-1", **_receipt_kwargs(f"click-{i}"))

    bundle = await signed_recorder.export_bundle("sess-1")
    assert bundle["receipt_count"] == 3
    assert bundle["public_key_b64"]
    assert bundle["head_hash"]

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(VERIFIER), str(bundle_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"standalone verifier rejected a good bundle:\n{result.stdout}\n{result.stderr}"
    assert "RESULT: VERIFIED" in result.stdout


@requires_verifier
@pytest.mark.asyncio
async def test_standalone_verifier_rejects_a_tampered_bundle(signed_recorder: WitnessRecorder, tmp_path: Path) -> None:
    await signed_recorder.startup()
    for i in range(3):
        await signed_recorder.record("sess-1", **_receipt_kwargs(f"click-{i}"))

    bundle = await signed_recorder.export_bundle("sess-1")
    bundle["receipts"][1]["action"] = "transfer-funds"

    bundle_path = tmp_path / "tampered.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(VERIFIER), str(bundle_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "RESULT: FAILED" in result.stdout


def test_list_with_a_zero_limit_returns_nothing(tmp_path: Path) -> None:
    """`lines[-0:]` is the whole list — a computed limit of 0 dumped the chain."""
    recorder = WitnessRecorder(tmp_path / "witness")
    path = recorder._path("sess-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")

    assert recorder._list_sync(path, 0) == []
    assert recorder._list_sync(path, -5) == []
