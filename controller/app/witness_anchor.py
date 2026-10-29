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

from .utils import atomic_write_text

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


def chain_records(data: bytes) -> list[str]:
    """The records of a chain's bytes: split on a newline only, blanks dropped.

    `str.splitlines()` also splits on U+2028, U+2029 and U+0085, which JSON leaves
    raw, so it cut a receipt holding one of them in two. Every receipt ends in a
    newline and JSON escapes every other control character. Blank means ASCII
    whitespace, the set `bytes.strip()` removes, in every reader. A record that
    is not valid UTF-8 comes back behind a NUL, which is never JSON, so every
    reader sees a record that does not parse. Decoding with replacement instead
    would let a flipped byte inside a string keep its hash and signature.
    """
    return [_decode_record(line.strip()) for line in data.split(b"\n") if line.strip()]


def _decode_record(record: bytes) -> str:
    try:
        return record.decode("utf-8")
    except UnicodeDecodeError:
        return "\x00" + record.decode("utf-8", errors="replace")


def read_records(handle: IO[Any]) -> list[str]:
    """Every record of an open chain file."""
    handle.flush()
    raw = handle.buffer
    raw.seek(0)
    return chain_records(raw.read())


def read_tail_line(handle: IO[Any]) -> str | None:
    """The last record of an open chain file, without reading all of it.

    Read as bytes: the window starts at a byte offset, which can fall inside a
    multi-byte character, and a text read would fail to decode it. Only the last
    record is decoded, and it is whole.
    """
    handle.flush()
    raw = handle.buffer
    size = raw.seek(0, os.SEEK_END)
    start = max(0, size - TAIL_READ_BYTES)
    raw.seek(start)
    lines = chain_records(raw.read())
    if start > 0 and len(lines) <= 1:
        # The tail window landed mid-record (or held only the end of one); read
        # the whole file rather than return a fragment that would not parse.
        lines = read_records(handle)
    return lines[-1] if lines else None


def anchor_path(chain_path: Path) -> Path:
    return chain_path.with_suffix(chain_path.suffix + ".anchor.json")


def write_anchor(chain_path: Path, *, head_hash: str, receipt_count: int) -> None:
    payload = {"head_hash": head_hash, "receipt_count": receipt_count}
    path = anchor_path(chain_path)
    atomic_write_text(path, json.dumps(payload, sort_keys=True))


def read_anchor(chain_path: Path) -> dict[str, Any] | None:
    path = anchor_path(chain_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):  # not JSON, not UTF-8, or unreadable
        return None
    return payload if isinstance(payload, dict) else None


def anchor_fields(anchor: dict[str, Any] | None) -> tuple[str, int] | None:
    """(head_hash, receipt_count) of an anchor that is well formed, else None.

    The count is an integer of at least 1 (not a bool, which is an int in Python)
    and the head a non-empty string: an anchor is written only after a receipt.
    """
    if anchor is None:
        return None
    head, count = anchor.get("head_hash"), anchor.get("receipt_count")
    if not isinstance(head, str) or not head:
        return None
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return None
    return head, count


def compare_to_anchor(chain_path: Path, *, head_hash: str | None, receipt_count: int) -> dict[str, Any]:
    """How the chain on disk stands against its anchor.

    `status` is `"ok"` only when the anchor is present, readable, and agrees with
    the chain. A missing anchor (`"anchor_missing"`) and an unreadable one
    (`"anchor_unreadable"`) are failures, not a pass: deleting or damaging the
    anchor after cutting the chain short would otherwise hide the cut. Chains
    written before anchors existed are anchored once, on the first start that
    finds them (`WitnessRecorder.startup`), and never again.
    """
    if not anchor_path(chain_path).exists():
        return _anchor_verdict(
            "anchor_missing", "the chain's anchor file is missing, so a cut-short chain cannot be told apart"
        )
    fields = anchor_fields(read_anchor(chain_path))
    if fields is None:
        return _anchor_verdict("anchor_unreadable", "the chain's anchor file is unreadable or malformed")

    expected_head, expected_count = fields
    if receipt_count < expected_count:
        reason = (
            f"chain holds {receipt_count} receipts but the anchor records {expected_count}; "
            "receipts have been removed from the end"
        )
        return _anchor_verdict("truncated", reason, expected_head, expected_count)
    if head_hash != expected_head or receipt_count != expected_count:
        reason = "chain head or length does not match the anchor; the chain was rewritten or forked"
        return _anchor_verdict("diverged", reason, expected_head, expected_count)
    return _anchor_verdict("ok", None, expected_head, expected_count)


def _anchor_verdict(
    status: str, reason: str | None, expected_head: str | None = None, expected_count: int | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "expected_head_hash": expected_head,
        "expected_receipt_count": expected_count,
    }
    if reason:
        result["reason"] = reason
    return result
