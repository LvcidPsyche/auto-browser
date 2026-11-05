"""One unparseable receipt line must not make the listing and the bundle unusable.

`list` and `export_bundle` raised on the first line that did not parse (the route answered 400 with no detail),
so a single bad line hid every good receipt. They now skip it and say how many they skipped; `verify` still
fails on the gap, so a bundle never claims to be intact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.browser_manager import BrowserManager
from app.witness import WitnessRecorder

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
