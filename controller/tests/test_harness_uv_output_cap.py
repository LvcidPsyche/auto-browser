"""The UV command's output is read with a cap, so a runaway command cannot fill the controller's memory."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.harness import TaskContract
from app.harness.verifier.uv import _STDERR_KEEP_BYTES, UniversalVerifierAdapter
from app.process_tree import GROUP_SPAWN_KWARGS, kill_and_drain, read_capped

CONTRACT = TaskContract(
    id="uv-cap",
    goal="uv output is bounded",
    postconditions=[{"kind": "url_contains", "value": "example.com"}],
)
TRACE = {"final_observation": {"url": "https://example.com"}}
VALID = "import json; print(json.dumps({'passed': True, 'confidence': 0.9}))"


class UvOutputCapTests(unittest.IsolatedAsyncioTestCase):
    async def _verify(self, code: str):
        started: list = []
        original = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            process = await original(*args, **kwargs)
            started.append(process)
            return process

        adapter = UniversalVerifierAdapter(command=[sys.executable, "-c", code], timeout_seconds=30)
        with mock.patch("app.harness.verifier.uv.asyncio.create_subprocess_exec", spy):
            result = await adapter.verify(CONTRACT, TRACE)
        return result, started

    async def test_output_over_the_cap_is_unavailable_and_the_child_is_reaped(self) -> None:
        result, started = await self._verify("import sys; sys.stdout.write('x' * 3_000_000)")

        self.assertIsNone(result.passed)
        self.assertFalse(result.details["available"])
        self.assertIn("exceeded", result.notes)
        self.assertIsNotNone(started[0].returncode)

    async def test_a_noisy_stderr_does_not_block_a_valid_answer(self) -> None:
        result, _ = await self._verify("import sys; sys.stderr.write('e' * 3_000_000); " + VALID)

        self.assertIs(result.passed, True)

    async def test_the_start_of_a_large_stderr_reaches_the_failure_notes(self) -> None:
        # R-11: a first read that crossed the 4 KiB cap was dropped whole, so the notes lost the traceback's start.
        result, _ = await self._verify(
            "import os, sys; os.write(2, b'Traceback-start: ' + b'E' * 5000); sys.exit(3)",
        )

        self.assertIn("code 3", result.notes)
        self.assertIn("Traceback-start", result.notes)

    async def test_a_chunk_that_crosses_the_cap_keeps_the_part_that_fits(self) -> None:
        stream = asyncio.StreamReader()
        data = bytes(index % 251 for index in range(5000))  # distinct bytes: the head, not any 4096 of them
        stream.feed_data(data)
        stream.feed_eof()

        kept = await read_capped(stream, _STDERR_KEEP_BYTES, strict=False)

        self.assertEqual(kept, data[:4096])

    async def test_a_normal_answer_is_unchanged(self) -> None:
        result, _ = await self._verify(VALID)

        self.assertIs(result.passed, True)
        self.assertEqual(result.confidence, 0.9)


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie nobody has reaped yet is not running
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


# A launcher that starts a grandchild sharing its stdout, then waits for it.
_LAUNCHER = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', sys.argv[1], *sys.argv[2:]]).wait()"
# Writes 64 KiB chunks for 20 s, counting what it wrote (a write blocks until the controller reads). The count is
# replaced atomically: the kill can land at any point, and an open(..., 'w') it interrupts leaves the file empty.
_WRITER = (
    "import os, sys, time\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "chunk, total, end = b'x' * 65536, 0, time.time() + 20\n"
    "while time.time() < end:\n"
    "    os.write(1, chunk)\n"
    "    total += len(chunk)\n"
    "    with open(sys.argv[2] + '.tmp', 'w') as count:\n"
    "        count.write(str(total))\n"
    "    os.replace(sys.argv[2] + '.tmp', sys.argv[2])\n"
)
# Holds stdout open without writing.
_HOLDER = "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(30)\n"


@unittest.skipUnless(hasattr(os, "killpg") and sys.platform != "darwin", "needs POSIX process groups")
class UvProcessGroupTests(unittest.IsolatedAsyncioTestCase):
    """R-17: only the direct child was killed, then both pipes were read to EOF with no cap and no timeout."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pid_file = self.root / "grandchild.pid"
        self.addCleanup(self._kill_leftover)

    def _kill_leftover(self) -> None:
        if self.pid_file.exists() and self.pid_file.read_text().strip():
            try:
                os.kill(int(self.pid_file.read_text()), 9)
            except ProcessLookupError:
                pass

    def _grandchild_gone_within(self, seconds: float) -> bool:
        pid = int(self.pid_file.read_text())
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and _is_running(pid):
            time.sleep(0.05)
        return not _is_running(pid)

    async def test_a_grandchild_writing_past_the_cap_is_killed_and_little_is_read(self) -> None:
        count_file = self.root / "written"
        command = [sys.executable, "-c", _LAUNCHER, _WRITER, str(self.pid_file), str(count_file)]
        adapter = UniversalVerifierAdapter(command=command, timeout_seconds=30)

        started = time.monotonic()
        result = await adapter.verify(CONTRACT, TRACE)

        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("exceeded", result.notes)
        self.assertTrue(self._grandchild_gone_within(2), "the grandchild outlived the kill")
        self.assertLess(int(count_file.read_text()), 2 * 1024 * 1024)

    async def test_a_grandchild_holding_stdout_does_not_hold_the_timeout(self) -> None:
        command = [sys.executable, "-c", _LAUNCHER, _HOLDER, str(self.pid_file)]
        adapter = UniversalVerifierAdapter(command=command, timeout_seconds=1)

        started = time.monotonic()
        result = await adapter.verify(CONTRACT, TRACE)

        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("timed out", result.notes)
        self.assertTrue(self._grandchild_gone_within(2), "the grandchild outlived the kill")

    async def test_a_descendant_that_left_the_group_ends_the_drain_at_its_timeout_with_a_warning(self) -> None:
        # setsid takes it out of the group, so the kill cannot reach it: the drain gives up instead of hanging.
        escapee = "import os, sys, time\nos.setsid()\n" + _HOLDER.split("\n", 1)[1]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _LAUNCHER,
            escapee,
            str(self.pid_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **GROUP_SPAWN_KWARGS,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (self.pid_file.exists() and self.pid_file.read_text()):
            await asyncio.sleep(0.05)

        started = time.monotonic()
        with self.assertLogs("app.process_tree", level="WARNING"):
            await kill_and_drain(process, label="test command", timeout=0.5)

        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(process.returncode)


if __name__ == "__main__":
    unittest.main()
