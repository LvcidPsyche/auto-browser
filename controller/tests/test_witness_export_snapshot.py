"""D1-3: an evidence bundle's receipts and its chain check come from one read of the chain.

export_bundle read the receipts, then verify read the chain again, with no lock between: an append that landed in
between gave a bundle whose head hash and verdict described a receipt its list did not hold.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app import witness as witness_module
from app.witness import WitnessRecorder
from tests.test_witness_signing import _receipt_kwargs

SCOPE = "sess-1"
# How long the export's read waits for the racing append; an export that holds the chain finishes without it.
RACE_WAIT_SECONDS = 1.0


async def test_an_append_cannot_land_between_the_bundles_receipts_and_its_chain_check(
    tmp_path: Path, monkeypatch
) -> None:
    recorder = WitnessRecorder(tmp_path / "witness")
    await recorder.startup()
    await recorder.record(SCOPE, **_receipt_kwargs("first"))
    loop = asyncio.get_running_loop()
    original = witness_module.chain_records
    racing: list[asyncio.Future] = []

    def chain_records_with_an_append(data: bytes) -> list[str]:
        records = original(data)
        if not racing:
            # The export has read the chain: another task appends now, and gets RACE_WAIT_SECONDS to finish.
            racing.append(asyncio.run_coroutine_threadsafe(recorder.record(SCOPE, **_receipt_kwargs("second")), loop))
            try:
                racing[0].result(timeout=RACE_WAIT_SECONDS)
            except TimeoutError:
                pass  # the append waits for the chain the export holds
        return records

    monkeypatch.setattr(witness_module, "chain_records", chain_records_with_an_append)
    bundle = await recorder.export_bundle(SCOPE)
    monkeypatch.setattr(witness_module, "chain_records", original)
    await asyncio.wrap_future(racing[0])

    assert [receipt["action"] for receipt in bundle["receipts"]] == ["first"]
    assert bundle["receipt_count"] == 1
    assert bundle["head_hash"] == bundle["receipts"][-1]["chain_hash"]
    assert bundle["chain_valid_at_export"] is True, bundle["chain_reason_at_export"]
    assert bundle["chain_anchor_status_at_export"] == "ok"
    verified = await recorder.verify(SCOPE)
    assert verified["valid"] and verified["receipt_count"] == 2  # the append went in after the export


async def test_exporting_a_scope_with_no_chain_creates_no_file(tmp_path: Path) -> None:
    recorder = WitnessRecorder(tmp_path / "witness")
    await recorder.startup()

    bundle = await recorder.export_bundle("never-recorded")

    assert bundle["receipts"] == [] and bundle["receipt_count"] == 0
    assert bundle["chain_valid_at_export"] is False
    assert bundle["chain_reason_at_export"] == "no receipts recorded for this scope"
    assert not recorder.has_evidence("never-recorded")
