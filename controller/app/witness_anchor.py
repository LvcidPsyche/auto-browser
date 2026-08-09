"""Cross-process append locking, and an anchor that makes truncation visible.

Two limits `witness_signing.py` documented in 1.6.0, both addressed here.

**Chain forking.** `WitnessRecorder` serialised appends with an `asyncio.Lock`,
which is process-local. Two workers — `AGENT_JOB_WORKER_COUNT > 1`, several
uvicorn workers, or two replicas on a shared volume — could read the same head
and write two receipts claiming the same predecessor. That is a correctness bug,
not a limit, so the read-head-and-append critical section now runs under an
OS-level lock on the chain file itself.

**Tail truncation.** Signing cannot detect it: drop the last k receipts and what
remains is a shorter chain whose signatures all still verify. Detecting it needs
a record kept somewhere the chain is not, so each append also updates a small
anchor file holding the head hash and receipt count. `verify()` compares the two.

An attacker who rewrites the anchor as well as the chain still defeats this —
that is what an *external* anchor is for, and the anchor file is deliberately a
separate artifact so one can be shipped elsewhere without changing the shape of
this. It is strictly better than "undetectable", and it needs no third party.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, Iterator

# How much of the chain tail to read when looking for the last receipt. Reading
# the whole file per append was O(n) in chain length; a receipt is well under
# this, so one read finds the last line in every realistic case.
TAIL_READ_BYTES = 64 * 1024

try:  # POSIX — the container
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # Windows — developer machines
    fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False

if not _HAVE_FCNTL:
    import msvcrt


@contextmanager
def exclusive_lock(handle: IO[Any]) -> Iterator[None]:
    """Hold an exclusive OS lock on an open file for the block's duration.

    Blocking on purpose: appends are short, and an append that gave up would
    either lose a receipt or fork the chain, which are the two outcomes this
    exists to prevent.
    """
    if _HAVE_FCNTL:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return

    handle.seek(0, os.SEEK_END)
    position = handle.tell()
    handle.seek(0)
    # Windows locks a byte range rather than the file. Byte 0 is arbitrary but
    # consistent, so every process contends for the same range.
    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    try:
        yield
    finally:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.seek(position)


def read_tail_line(handle: IO[Any]) -> str | None:
    """The last non-empty line of an open text file, without reading all of it."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    if size == 0:
        return None
    start = max(0, size - TAIL_READ_BYTES)
    handle.seek(start)
    chunk = handle.read()
    lines = [line for line in chunk.splitlines() if line.strip()]
    if not lines:
        return None
    if start > 0 and len(lines) == 1:
        # The tail window landed mid-record; fall back to a full read rather
        # than returning a fragment that would not parse.
        handle.seek(0)
        lines = [line for line in handle.read().splitlines() if line.strip()]
    return lines[-1] if lines else None


def anchor_path(chain_path: Path) -> Path:
    return chain_path.with_suffix(chain_path.suffix + ".anchor.json")


def write_anchor(chain_path: Path, *, head_hash: str, receipt_count: int) -> None:
    payload = {"head_hash": head_hash, "receipt_count": receipt_count}
    path = anchor_path(chain_path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def read_anchor(chain_path: Path) -> dict[str, Any] | None:
    path = anchor_path(chain_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def compare_to_anchor(chain_path: Path, *, head_hash: str | None, receipt_count: int) -> dict[str, Any]:
    """How the chain on disk stands against its anchor.

    `status` is `"unanchored"` for chains written before this release — absence
    of an anchor is not evidence of tampering, and must not be reported as if it
    were.
    """
    anchor = read_anchor(chain_path)
    if anchor is None:
        return {"status": "unanchored", "expected_head_hash": None, "expected_receipt_count": None}

    expected_head = anchor.get("head_hash")
    expected_count = anchor.get("receipt_count")
    result = {
        "status": "ok",
        "expected_head_hash": expected_head,
        "expected_receipt_count": expected_count,
    }
    if isinstance(expected_count, int) and receipt_count < expected_count:
        result["status"] = "truncated"
        result["reason"] = (
            f"chain holds {receipt_count} receipts but the anchor records {expected_count}; "
            "receipts have been removed from the end"
        )
    elif expected_head and head_hash != expected_head:
        result["status"] = "diverged"
        result["reason"] = "chain head does not match the anchor; the chain was rewritten or forked"
    return result
