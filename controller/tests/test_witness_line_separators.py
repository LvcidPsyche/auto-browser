"""A receipt holding U+2028, U+2029 or U+0085 is still one line of the chain.

JSON leaves those characters raw, and `str.splitlines()` treats them as line ends,
so a reader that used it cut such a receipt in two. Every receipt ends in "\\n" and
JSON escapes every other control character, so "\\n" alone separates records.
A page reaches this through its title or text, which receipts copy.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.witness import WitnessRecorder
from app.witness_signing import WitnessSigner
from tests.test_witness_missing_evidence import VERIFIER
from tests.test_witness_signing import _receipt_kwargs

SCOPE = "sess-1"
SEPARATORS = ["\u2028", "\u2029", "\u0085"]


@pytest.fixture
async def recorder(tmp_path: Path) -> WitnessRecorder:
    recorder = WitnessRecorder(tmp_path / "witness", signer=WitnessSigner(tmp_path / "keys"))
    await recorder.startup()
    for index, separator in enumerate([*SEPARATORS, ""]):
        kwargs = _receipt_kwargs(f"click-{index}")
        kwargs["metadata"] = {"title": f"Sale{separator}ends today"}
        await recorder.record(SCOPE, **kwargs)
    return recorder


async def test_verify_walks_all_four_receipts(recorder: WitnessRecorder) -> None:
    result = await recorder.verify(SCOPE)

    assert result["valid"] is True, result["reason"]
    assert result["receipt_count"] == 4


async def test_signatures_cover_all_four_receipts(recorder: WitnessRecorder) -> None:
    result = await recorder.verify_signatures(SCOPE)

    assert result["valid"] is True, result["reason"]
    assert result["checked"] == 4


async def test_list_returns_all_four_in_order(recorder: WitnessRecorder) -> None:
    receipts = await recorder.list(SCOPE, limit=10)

    assert [item.action for item in reversed(receipts)] == [f"click-{index}" for index in range(4)]
    assert [item.metadata["title"] for item in reversed(receipts)][:3] == [f"Sale{s}ends today" for s in SEPARATORS]


async def test_list_still_skips_a_torn_last_record(recorder: WitnessRecorder) -> None:
    with recorder._path(SCOPE).open("a", encoding="utf-8") as handle:
        handle.write('{"receipt_id": "cut sh')

    assert len(await recorder.list(SCOPE, limit=10)) == 4


async def test_export_bundle_holds_all_four_and_the_standalone_verifier_accepts_it(
    recorder: WitnessRecorder, tmp_path: Path
) -> None:
    bundle = await recorder.export_bundle(SCOPE)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(VERIFIER), str(path)], capture_output=True, text=True, timeout=60
    )

    assert bundle["receipt_count"] == 4
    assert bundle["chain_valid_at_export"] is True
    assert result.returncode == 0, result.stdout


async def test_a_chain_a_reader_once_cut_in_two_takes_appends_and_verifies(recorder: WitnessRecorder) -> None:
    """The bytes on disk were always fine; a fresh recorder (no cached head) carries on."""
    fresh = WitnessRecorder(recorder.root, signer=recorder.signer)

    await fresh.record(SCOPE, **_receipt_kwargs("click-4"))

    result = await fresh.verify(SCOPE)
    assert result["valid"] is True, result["reason"]
    assert result["receipt_count"] == 5
