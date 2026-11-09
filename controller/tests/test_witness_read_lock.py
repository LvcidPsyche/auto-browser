"""A6: a witness chain's locked read (export, verify) works where flock is emulated with POSIX locks.

The read took an exclusive flock on a read-only handle. On Linux over NFS, flock is emulated with POSIX byte-range
locks, and an exclusive one needs a descriptor open for writing: the export and the chain check failed there with
EBADF. The fake fcntl below enforces that rule (nothing here mounts NFS), and records which lock each handle took.
"""

from __future__ import annotations

import builtins
import errno
from pathlib import Path

import pytest

from app import witness as witness_module
from app import witness_anchor
from app.witness import WitnessRecorder
from app.witness_signing import WitnessSigner
from tests.test_witness_signing import _receipt_kwargs

SCOPE = "sess-1"


class _NfsEmulatedFlock:
    """flock as Linux's NFS client emulates it: an exclusive lock on a descriptor not open for writing is EBADF."""

    LOCK_SH, LOCK_EX, LOCK_UN = 1, 2, 8

    def __init__(self) -> None:
        self.modes: dict[int, str] = {}  # each open handle's mode, by descriptor
        self.taken: list[tuple[str, int]] = []  # (mode, lock) for every lock taken

    def flock(self, fd: int, operation: int) -> None:
        if operation == self.LOCK_UN:
            return
        mode = self.modes[fd]
        if operation == self.LOCK_EX and not any(flag in mode for flag in "wax+"):
            raise OSError(errno.EBADF, "Bad file descriptor")
        self.taken.append((mode, operation))


@pytest.fixture
def nfs_flock(monkeypatch) -> _NfsEmulatedFlock:
    fake = _NfsEmulatedFlock()

    def recording_open(file, mode="r", *args, **kwargs):
        handle = builtins.open(file, mode, *args, **kwargs)
        fake.modes[handle.fileno()] = mode
        return handle

    monkeypatch.setattr(witness_anchor, "_HAVE_FCNTL", True)
    monkeypatch.setattr(witness_anchor, "fcntl", fake, raising=False)
    monkeypatch.setattr(witness_module, "open", recording_open, raising=False)
    return fake


async def test_the_locked_read_takes_a_lock_a_read_only_handle_can_hold_and_an_append_waits_for(
    tmp_path: Path, nfs_flock: _NfsEmulatedFlock
) -> None:
    recorder = WitnessRecorder(tmp_path / "witness", signer=WitnessSigner(tmp_path / "keys"))
    await recorder.startup()
    await recorder.record(SCOPE, **_receipt_kwargs("first"))
    appended = list(nfs_flock.taken)
    nfs_flock.taken.clear()

    bundle = await recorder.export_bundle(SCOPE)
    verified = await recorder.verify_with_signatures(SCOPE)

    assert bundle["receipt_count"] == 1 and bundle["chain_valid_at_export"] is True
    assert verified["valid"] is True, verified["reason"]
    # The reads hold a shared lock on their read-only handle, which an append's exclusive one excludes.
    assert nfs_flock.taken == [("rb", _NfsEmulatedFlock.LOCK_SH)] * 2
    assert appended == [("a+", _NfsEmulatedFlock.LOCK_EX)]
