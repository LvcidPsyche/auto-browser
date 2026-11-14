"""`run_cli` starts a provider CLI in its own process group, reads it with a cap and stops all of it (STORE-34).

It started the child in the controller's own group, killed only the child on a timeout or a cancel, waited for the
pipes with no bound after the kill (a grandchild that held them kept the call for as long as it ran), and buffered
whatever the child wrote.

The tests that need a real grandchild in the child's group are POSIX-only: Windows has no process groups here
(`process_tree.GROUPED` is false), so the kill reaches the child alone and a grandchild outlives it. What a grandchild
that holds the pipes costs there is only the bound of the drain, which the cross-platform test holds.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.process_tree import DRAIN_TIMEOUT_SECONDS, GROUPED, kill_tree
from app.providers.base import CLI_STDERR_KEEP_BYTES, ProviderAPIError
from app.providers.claude_adapter import ClaudeAdapter
from tests.test_harness_uv_output_cap import _is_running

TIMEOUT_SECONDS = 3
# A launcher that starts a grandchild sharing its stdout and stderr, then waits for it. The handles are named, so that
# the grandchild holds the pipes on Windows too.
_LAUNCHER = (
    "import subprocess, sys; "
    "subprocess.Popen([sys.executable, '-c', sys.argv[1], *sys.argv[2:]], stdout=sys.stdout, stderr=sys.stderr).wait()"
)
# Records its pid, then holds the pipes it inherited open and sleeps.
_HOLDER = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(20)"
posix_only = unittest.skipUnless(GROUPED and sys.platform != "darwin", "needs POSIX process groups")


class RunCliTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Where the kill cannot reach a grandchild (Windows) it still has this as its working directory.
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pid_file = self.root / "grandchild.pid"
        self.addCleanup(self._kill_leftover)
        settings = Settings(_env_file=None)
        settings.model_request_timeout_seconds = TIMEOUT_SECONDS
        self.adapter = ClaudeAdapter(settings)

    def _kill_leftover(self) -> None:
        if self.pid_file.exists() and self.pid_file.read_text().strip():
            try:
                os.kill(int(self.pid_file.read_text()), 9)
            except OSError:  # gone already (ProcessLookupError; on Windows an OSError for a pid that is not there)
                pass

    async def _run(self, code: str, *args: str, input_text: str | None = None):
        # An env and a cwd of its own: neither the controller's environment nor a CLI home under the real home.
        return await self.adapter.run_cli(
            command=[sys.executable, "-c", code, *args],
            input_text=input_text,
            env=dict(os.environ),
            cwd=str(self.root),
        )

    async def _run_holding_the_pipes(self):
        return await self._run(_LAUNCHER, _HOLDER, str(self.pid_file))

    async def _grandchild_gone_within(self, seconds: float) -> bool:
        pid = int(self.pid_file.read_text())
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and _is_running(pid):
            await asyncio.sleep(0.05)
        return not _is_running(pid)

    async def _started(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (self.pid_file.exists() and self.pid_file.read_text()):
            await asyncio.sleep(0.05)
        self.assertTrue(self.pid_file.exists() and self.pid_file.read_text(), "the grandchild did not start")

    def _spy_on_spawns(self):
        started: list[asyncio.subprocess.Process] = []
        real = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            process = await real(*args, **kwargs)
            started.append(process)
            return process

        return started, patch("app.providers.base.asyncio.create_subprocess_exec", spy)

    async def test_a_cli_that_answers_returns_its_output_and_reads_its_input(self) -> None:
        result = await self._run("import sys; sys.stdout.write(sys.stdin.read().upper())", input_text="abc")

        self.assertEqual((result.stdout, result.returncode), ("ABC", 0))

    async def test_a_cli_that_fails_is_a_provider_error_with_the_start_of_its_stderr(self) -> None:
        with self.assertRaises(ProviderAPIError) as caught:
            await self._run("import sys; sys.stderr.write('boom: bad flag'); sys.exit(3)")

        self.assertIn("boom: bad flag", caught.exception.message)
        self.assertFalse(caught.exception.retryable)

    async def test_stdout_past_the_cap_is_an_error_and_the_child_is_reaped(self) -> None:
        started, spy = self._spy_on_spawns()

        with patch("app.providers.base.CLI_STDOUT_LIMIT_BYTES", 100_000), spy:
            with self.assertRaises(ProviderAPIError) as caught:
                await self._run("import sys; sys.stdout.write('x' * 3_000_000)")

        self.assertIn("exceeded 100000 bytes", caught.exception.message)
        self.assertFalse(caught.exception.retryable)
        self.assertIsNotNone(started[0].returncode)

    async def test_a_noisy_stderr_blocks_nothing_and_only_its_start_is_kept(self) -> None:
        result = await self._run("import sys; sys.stderr.write('e' * 3_000_000); sys.stdout.write('ok')")

        self.assertEqual(result.stdout, "ok")
        self.assertEqual(result.stderr, "e" * CLI_STDERR_KEEP_BYTES)

    async def test_a_grandchild_holding_the_pipes_does_not_hold_the_timeout_past_the_drain(self) -> None:
        # Where the kill reaches the grandchild (POSIX) the call ends at its timeout. Where it does not (Windows),
        # the grandchild keeps the pipes, and the call ends when the drain gives up on them: never when it exits.
        started = time.monotonic()
        with self.assertRaises(ProviderAPIError) as caught:
            await self._run_holding_the_pipes()

        self.assertLess(time.monotonic() - started, TIMEOUT_SECONDS + DRAIN_TIMEOUT_SECONDS + 5)
        self.assertIn("timed out", caught.exception.message)
        self.assertTrue(caught.exception.retryable)

    @posix_only
    async def test_a_timeout_kills_the_grandchild_too_and_returns_within_seconds(self) -> None:
        started = time.monotonic()
        with self.assertRaises(ProviderAPIError) as caught:
            await self._run_holding_the_pipes()

        self.assertLess(time.monotonic() - started, TIMEOUT_SECONDS + 3)
        self.assertIn("timed out", caught.exception.message)
        await self._started()
        self.assertTrue(await self._grandchild_gone_within(2), "the grandchild outlived the timeout")

    @posix_only
    async def test_a_cancelled_call_kills_the_whole_group_and_reaps_it(self) -> None:
        started, spy = self._spy_on_spawns()
        with spy:
            task = asyncio.create_task(self._run_holding_the_pipes())
            await self._started()
            # The grandchild can be up before the spawn has returned (a process's first spawn is slow). A cancel that
            # lands there is asyncio's to clean up, not run_cli's, and is not what this test is about.
            while not started:
                await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(await self._grandchild_gone_within(2), "the grandchild outlived the cancel")
        self.assertIsNotNone(started[0].returncode, "the child was not reaped")

    async def test_a_second_cancel_while_the_child_is_being_reaped_does_not_leave_it_unreaped(self) -> None:
        # The reaping is shielded, as it was: a caller cancelled again meanwhile (a shutdown over a cancel) still
        # leaves the child stopped.
        started, spy = self._spy_on_spawns()
        reaped = asyncio.Event()

        async def slow_kill_and_drain(process, *, label: str = "command") -> None:
            kill_tree(process)
            await asyncio.sleep(0.3)
            await process.wait()
            reaped.set()

        with spy, patch("app.providers.base.kill_and_drain", slow_kill_and_drain):
            task = asyncio.create_task(self._run("import time; time.sleep(30)"))
            while not started:
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)
            task.cancel()
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            await asyncio.wait_for(reaped.wait(), 5)

        self.assertIsNotNone(started[0].returncode)

    async def test_a_prompt_that_cannot_be_encoded_starts_no_child(self) -> None:
        # A page's lone surrogate reaches the prompt. The child was started first, and the error then left it running
        # with its stdin open.
        started, spy = self._spy_on_spawns()

        with spy, self.assertRaises(UnicodeEncodeError):
            await self._run("import sys; sys.stdin.read()", input_text="a page said \ud800")

        self.assertEqual(started, [])


if __name__ == "__main__":
    unittest.main()
