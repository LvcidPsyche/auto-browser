"""The two witness limits 1.6.0 documented, now tested as behaviour.

Tail truncation: signing cannot detect it. Drop the last k receipts and what
remains is a shorter chain whose every signature still verifies, so `verify()`
reported a clean chain. The anchor is a record of where the chain had got to,
kept beside it, so a short chain stops looking like a complete one.

Chain forking: appends were serialised with an `asyncio.Lock`, which orders one
process. Two workers on a shared volume could read the same head and write two
receipts claiming the same predecessor — a correctness bug, not a limit.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from app.models import OperatorIdentity
from app.witness import WitnessReceipt, WitnessRecorder
from app.witness_anchor import anchor_path, read_anchor


def receipt(action: str) -> WitnessReceipt:
    return WitnessReceipt(
        receipt_id=action,
        timestamp="2026-08-08T00:00:00Z",
        profile="normal",
        scope="sess-1",
        event_type="action",
        status="ok",
        action=action,
        action_class="read",
        operator=OperatorIdentity(id="alice", source="token"),
    )


@pytest.fixture()
def recorder(tmp_path_factory) -> WitnessRecorder:
    root = Path(tempfile.mkdtemp(prefix="auto-browser-witness-anchor-"))
    try:
        yield WitnessRecorder(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_the_anchor_tracks_the_head_and_count(recorder: WitnessRecorder) -> None:
    await recorder.startup()
    for index in range(3):
        written = await recorder.record("sess-1", **receipt(f"click-{index}").model_dump(exclude={"scope"}))

    anchor = read_anchor(recorder._path("sess-1"))
    assert anchor == {"head_hash": written.chain_hash, "receipt_count": 3}


@pytest.mark.asyncio
async def test_truncating_the_tail_is_detected(recorder: WitnessRecorder) -> None:
    await recorder.startup()
    for index in range(4):
        await recorder.record("sess-1", **receipt(f"click-{index}").model_dump(exclude={"scope"}))

    path = recorder._path("sess-1")
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")

    result = await recorder.verify("sess-1")
    # The surviving receipts are a perfectly valid chain — that is the point.
    assert result["first_invalid_index"] is None
    assert result["anchor"]["status"] == "truncated"
    assert result["anchor"]["expected_receipt_count"] == 4
    assert result["valid"] is False
    assert "removed from the end" in result["reason"]


@pytest.mark.asyncio
async def test_a_chain_with_no_anchor_is_not_reported_as_tampered(recorder: WitnessRecorder) -> None:
    """Chains written before this release must not start failing verification."""
    await recorder.startup()
    await recorder.record("sess-1", **receipt("click-0").model_dump(exclude={"scope"}))
    anchor_path(recorder._path("sess-1")).unlink()

    result = await recorder.verify("sess-1")
    assert result["anchor"]["status"] == "unanchored"
    assert result["valid"] is True


@pytest.mark.asyncio
async def test_an_intact_chain_still_verifies(recorder: WitnessRecorder) -> None:
    await recorder.startup()
    for index in range(3):
        await recorder.record("sess-1", **receipt(f"click-{index}").model_dump(exclude={"scope"}))

    result = await recorder.verify("sess-1")
    assert result["valid"] is True
    assert result["anchor"]["status"] == "ok"
    assert result["receipt_count"] == 3


def test_concurrent_writers_cannot_fork_the_chain() -> None:
    """Threads bypass the asyncio lock, so this exercises the file lock itself.

    Without it, two writers read the same head and both claim it as their
    predecessor, and verify() reports a broken chain at the second one.
    """
    root = Path(tempfile.mkdtemp(prefix="auto-browser-witness-fork-"))
    try:
        recorder = WitnessRecorder(root)
        asyncio.run(recorder.startup())
        path = recorder._path("sess-1")

        writers = 12
        barrier = threading.Barrier(writers)

        def append(index: int) -> None:
            item = receipt(f"click-{index}")
            barrier.wait()
            recorder._append_locked(path, item)

        threads = [threading.Thread(target=append, args=(index,)) for index in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) == writers, "every append must survive"

        result = asyncio.run(recorder.verify("sess-1"))
        assert result["valid"] is True, result.get("reason")
        assert result["receipt_count"] == writers
        assert json.loads(lines[-1])["chain_hash"] == read_anchor(path)["head_hash"]
    finally:
        shutil.rmtree(root, ignore_errors=True)
