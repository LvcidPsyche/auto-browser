"""One unparseable receipt line must not make the listing and the bundle unusable.

`list` and `export_bundle` raised on the first line that did not parse (the route answered 400 with no detail),
so a single bad line hid every good receipt. They now skip it and say how many they skipped; `verify` still
fails on the gap, so a bundle never claims to be intact.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.browser_manager import BrowserManager
from app.witness import WitnessRecorder
from app.witness_signing import WitnessSigner
from tests.test_witness_signing import VERIFIER, _receipt_kwargs, requires_verifier

SCOPE = "sess-1"


@pytest.fixture
async def recorder(tmp_path: Path) -> WitnessRecorder:
    recorder = WitnessRecorder(tmp_path / "witness")
    await recorder.startup()
    for index in range(3):
        await recorder.record(
            SCOPE,
            profile="normal",
            event_type="browser_action",
            status="ok",
            action=f"click-{index}",
            action_class="write",
            operator={"id": "a", "source": "header"},
            metadata={},
        )
    # A garbage line between the first and the second receipt.
    path = recorder._path(SCOPE)
    first, rest = path.read_bytes().split(b"\n", 1)
    path.write_bytes(first + b"\n{this is not json}\n" + rest)
    return recorder


async def test_list_skips_the_bad_line_and_counts_it(recorder: WitnessRecorder) -> None:
    receipts = await recorder.list(SCOPE, limit=10)

    assert [item.action for item in receipts] == ["click-2", "click-1", "click-0"]
    assert receipts.unparseable_lines == 1


async def test_list_reports_zero_when_every_line_parses(tmp_path: Path) -> None:
    clean = WitnessRecorder(tmp_path / "clean")
    await clean.startup()
    await clean.record(
        SCOPE,
        profile="normal",
        event_type="browser_action",
        status="ok",
        action="click",
        action_class="write",
        operator={"id": "a", "source": "header"},
        metadata={},
    )

    assert (await clean.list(SCOPE, limit=10)).unparseable_lines == 0


async def test_export_bundle_keeps_the_good_receipts_and_marks_the_gap(recorder: WitnessRecorder) -> None:
    bundle = await recorder.export_bundle(SCOPE)

    assert bundle["receipt_count"] == 3
    assert bundle["unparseable_lines"] == 1
    assert bundle["chain_valid_at_export"] is False


async def test_the_chain_check_still_fails_on_the_gap(recorder: WitnessRecorder) -> None:
    result = await recorder.verify(SCOPE)

    assert result["valid"] is False
    assert result["reason"] == "receipt is not parseable"


async def test_the_manager_listing_carries_the_count(tmp_path: Path, recorder: WitnessRecorder) -> None:
    manager = object.__new__(BrowserManager)
    manager.witness = recorder

    listing = await manager.list_witness_receipts(SCOPE, limit=10)

    assert len(listing) == 3
    assert listing.unparseable_lines == 1


def _load_verifier():
    spec = importlib.util.spec_from_file_location("verify_witness_bundle_under_test", VERIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_verify_bundle():
    return _load_verifier().verify_bundle


@pytest.fixture
async def signed_chain(tmp_path: Path) -> WitnessRecorder:
    recorder = WitnessRecorder(tmp_path / "witness", signer=WitnessSigner(tmp_path / "keys"))
    await recorder.startup()
    for index in range(3):
        await recorder.record(SCOPE, **_receipt_kwargs(f"click-{index}"))
    return recorder


@requires_verifier
async def test_a_clean_signed_bundle_passes_the_offline_verifier(signed_chain: WitnessRecorder) -> None:
    bundle = await signed_chain.export_bundle(SCOPE)

    result = _load_verify_bundle()(bundle)

    assert result["ok"], result["failures"]
    assert bundle["head_hash"] and bundle["unparseable_lines"] == 0


@requires_verifier
async def test_a_bundle_whose_last_line_is_corrupt_fails_the_offline_verifier(signed_chain: WitnessRecorder) -> None:
    path = signed_chain._path(SCOPE)
    path.write_bytes(path.read_bytes() + b"{}\n")

    bundle = await signed_chain.export_bundle(SCOPE)
    result = _load_verify_bundle()(bundle)

    assert (bundle["receipt_count"], bundle["unparseable_lines"], bundle["chain_valid_at_export"]) == (3, 1, False)
    # No head is claimed next to a receipt list that has a gap at its end.
    assert bundle["head_hash"] is None
    assert result["ok"] is False
    assert any("unparseable_lines" in failure for failure in result["failures"]), result["failures"]
    assert any("chain_valid_at_export" in failure for failure in result["failures"]), result["failures"]


@requires_verifier
async def test_the_unparseable_count_alone_fails_a_bundle(signed_chain: WitnessRecorder) -> None:
    bundle = await signed_chain.export_bundle(SCOPE)
    bundle["unparseable_lines"] = 2

    result = _load_verify_bundle()(bundle)

    assert result["ok"] is False
    assert any("unparseable_lines is 2" in failure for failure in result["failures"]), result["failures"]


async def _unsigned_chain(tmp_path: Path, count: int = 3) -> WitnessRecorder:
    recorder = WitnessRecorder(tmp_path / "unsigned-witness")
    await recorder.startup()
    for index in range(count):
        await recorder.record(SCOPE, **_receipt_kwargs(f"click-{index}"))
    return recorder


@requires_verifier
async def test_a_clean_unsigned_bundle_says_valid_and_passes_when_allowed(tmp_path: Path) -> None:
    bundle = await (await _unsigned_chain(tmp_path)).export_bundle(SCOPE)

    result = _load_verify_bundle()(bundle, allow_unsigned=True)

    assert bundle["chain_valid_at_export"] is True
    assert result["ok"], result["failures"]


@requires_verifier
async def test_an_unsigned_chain_truncated_by_one_line_fails_even_when_unsigned_is_allowed(tmp_path: Path) -> None:
    recorder = await _unsigned_chain(tmp_path)
    path = recorder._path(SCOPE)
    path.write_bytes(b"".join(path.read_bytes().splitlines(keepends=True)[:-1]))

    bundle = await recorder.export_bundle(SCOPE)
    result = _load_verify_bundle()(bundle, allow_unsigned=True)

    assert (bundle["unparseable_lines"], bundle["chain_valid_at_export"]) == (0, False)
    assert bundle["chain_reason_at_export"]
    assert result["ok"] is False
    assert any("chain_valid_at_export" in failure for failure in result["failures"]), result["failures"]
    assert any(bundle["chain_reason_at_export"] in failure for failure in result["failures"]), result["failures"]


@requires_verifier
@pytest.mark.parametrize("value", [1.0, "1", True, None, [], 2])
async def test_unparseable_lines_must_be_the_integer_zero(signed_chain: WitnessRecorder, value: object) -> None:
    bundle = await signed_chain.export_bundle(SCOPE)
    bundle["unparseable_lines"] = value

    result = _load_verify_bundle()(bundle)

    assert result["ok"] is False
    assert any("unparseable_lines" in failure for failure in result["failures"]), result["failures"]


async def test_a_torn_tail_is_skipped_by_list_and_export_alike_and_not_counted(tmp_path: Path) -> None:
    recorder = await _unsigned_chain(tmp_path)
    path = recorder._path(SCOPE)
    path.write_bytes(path.read_bytes() + b'{"receipt_id": "an append cut sh')  # no newline: never finished

    listing = await recorder.list(SCOPE, limit=10)
    bundle = await recorder.export_bundle(SCOPE)

    assert (len(listing), listing.unparseable_lines) == (3, 0)
    assert (bundle["receipt_count"], bundle["unparseable_lines"], bundle["torn_tail"]) == (3, 0, True)
    assert bundle["head_hash"] == bundle["receipts"][-1]["chain_hash"]
    # The chain check still reads the fragment as a record that does not parse, until the next append sets it aside.
    assert (bundle["chain_valid_at_export"], bundle["chain_reason_at_export"]) == (False, "receipt is not parseable")


async def test_a_bundle_without_a_torn_tail_says_so(tmp_path: Path) -> None:
    bundle = await (await _unsigned_chain(tmp_path)).export_bundle(SCOPE)

    assert bundle["torn_tail"] is False


@requires_verifier
async def test_a_signed_chain_truncated_by_one_line_fails_on_chain_valid_at_export_alone(
    signed_chain: WitnessRecorder,
) -> None:
    path = signed_chain._path(SCOPE)
    path.write_bytes(b"".join(path.read_bytes().splitlines(keepends=True)[:-1]))

    bundle = await signed_chain.export_bundle(SCOPE)
    result = _load_verify_bundle()(bundle)

    # Nothing unparseable, a head that matches the receipts left and good signatures: only the exporter's own
    # chain check (the anchor reads truncated) catches the cut.
    assert (bundle["unparseable_lines"], bundle["chain_valid_at_export"]) == (0, False)
    assert bundle["chain_anchor_status_at_export"] == "truncated"
    assert bundle["head_hash"] == bundle["receipts"][-1]["chain_hash"]
    assert len(result["failures"]) == 1, result["failures"]
    assert "chain_valid_at_export" in result["failures"][0]
    assert bundle["chain_reason_at_export"] in result["failures"][0]


@requires_verifier
@pytest.mark.parametrize("value", [None, 0, 1, "false", "true"])
async def test_chain_valid_at_export_must_be_true_when_present(signed_chain: WitnessRecorder, value: object) -> None:
    bundle = await signed_chain.export_bundle(SCOPE)
    bundle["chain_valid_at_export"] = value

    result = _load_verify_bundle()(bundle)

    assert result["ok"] is False
    assert any("chain_valid_at_export" in failure for failure in result["failures"]), result["failures"]


@requires_verifier
async def test_a_bundle_from_before_chain_valid_at_export_still_passes(signed_chain: WitnessRecorder) -> None:
    bundle = await signed_chain.export_bundle(SCOPE)
    for field in ("chain_valid_at_export", "chain_reason_at_export", "chain_anchor_status_at_export"):
        del bundle[field]

    result = _load_verify_bundle()(bundle)

    assert result["ok"], result["failures"]


@requires_verifier
async def test_the_verifier_prints_bundle_strings_escaped_and_capped(
    signed_chain: WitnessRecorder, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = await signed_chain.export_bundle(SCOPE)
    hostile = "\x1b[8m\rRESULT: VERIFIED\n\u202e" + "A" * 5000
    for field in ("scope", "algorithm", "signing_key_id", "head_hash", "receipt_count", "chain_reason_at_export"):
        bundle[field] = hostile
    bundle["chain_valid_at_export"] = False
    bundle["receipts"][0]["receipt_id"] = hostile
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    code = _load_verifier().main(["verify_witness_bundle.py", str(bundle_path)])
    lines = capsys.readouterr().out.splitlines()

    assert code == 1
    assert lines[-1] == "RESULT: FAILED"
    assert not any(line.startswith("RESULT: VERIFIED") for line in lines)
    assert all(character.isprintable() for line in lines for character in line), lines
    assert any("\\x1b[8m\\rRESULT: VERIFIED\\n\\u202e" in line for line in lines)
    assert max(len(line) for line in lines) < 1000
