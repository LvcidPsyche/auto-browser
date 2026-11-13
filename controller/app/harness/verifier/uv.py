from __future__ import annotations

import asyncio
import json
from shutil import which
from typing import Any

from ...process_tree import GROUP_SPAWN_KWARGS, OutputTooLarge, exchange, kill_and_drain
from ...providers.base import allowlisted_environment
from ...utils import error_text, without_lone_surrogates
from ..contracts import TaskContract
from .base import VerificationResult

# The command's answer is a small JSON object; anything past this is a runaway or hostile command.
_STDOUT_LIMIT_BYTES = 1024 * 1024
# Only the start of stderr is kept (it feeds a 1 KiB failure detail); the rest is drained so the child never blocks.
_STDERR_KEEP_BYTES = 4096


class UniversalVerifierAdapter:
    """Explicit dependency-checking adapter for external Universal Verifier backends."""

    backend = "uv"

    def __init__(
        self,
        command: list[str] | None = None,
        timeout_seconds: float = 60.0,
        env_passthrough: list[str] | tuple[str, ...] = (),
    ):
        self.command = command
        self.timeout_seconds = timeout_seconds
        # HARNESS_UV_ENV_PASSTHROUGH: names the command gets on top of the base ones (W2-27).
        self.env_passthrough = tuple(env_passthrough)

    @property
    def available(self) -> bool:
        return bool(self.command and which(self.command[0]))

    async def verify(self, contract: TaskContract, trace: Any) -> VerificationResult:
        if not self.command:
            return self._unavailable("Universal Verifier command is not configured.")
        if not which(self.command[0]):
            return self._unavailable(f"Universal Verifier command was not found: {self.command[0]}")

        payload = {
            "contract": contract.model_dump(mode="json"),
            "trace": trace.model_dump(mode="json") if hasattr(trace, "model_dump") else trace,
        }

        process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # The base names and HARNESS_UV_ENV_PASSTHROUGH, not the controller's tokens and keys (W2-27).
            env=allowlisted_environment(self.env_passthrough),
            **GROUP_SPAWN_KWARGS,  # its own process group on POSIX, so the kill reaches what it started
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                exchange(
                    process,
                    json.dumps(payload).encode("utf-8"),
                    stdout_limit=_STDOUT_LIMIT_BYTES,
                    stderr_keep=_STDERR_KEEP_BYTES,
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            await self._kill_and_reap(process)
            return self._unavailable("Universal Verifier command timed out.")
        except OutputTooLarge:
            await self._kill_and_reap(process)
            return self._unavailable(f"Universal Verifier output exceeded {_STDOUT_LIMIT_BYTES} bytes.")
        except BaseException:
            # The caller gave up (HarnessService wraps verify() in a wall-clock
            # wait_for) or the app is shutting down: CancelledError is not a
            # TimeoutError, so the child used to keep running, unreaped.
            await self._kill_and_reap(process)
            raise

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[:1024]
            return self._unavailable(
                f"Universal Verifier command failed (code {process.returncode}): {detail}",
            )

        try:
            result = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return self._unavailable("Universal Verifier command returned invalid JSON output.")

        if not isinstance(result, dict):
            return self._unavailable("Universal Verifier output shape is invalid.")

        try:
            # json.loads decodes a "\ud800" escape to a lone surrogate, which nothing that keeps the result can be
            # written with (run.json, drift.json, a candidate) and no string the result types takes. Replaced here,
            # where the reply is taken in, in its keys too.
            result = without_lone_surrogates(result)
            return VerificationResult(
                passed=result.get("passed"),
                confidence=float(result.get("confidence") or 0.0),
                failed_postconditions=[str(item) for item in result.get("failed_postconditions", [])],
                satisfied_postconditions=[str(item) for item in result.get("satisfied_postconditions", [])],
                forbidden_state_hits=[str(item) for item in result.get("forbidden_state_hits", [])],
                missing_evidence=[str(item) for item in result.get("missing_evidence", [])],
                notes=str(result.get("notes") or "Universal Verifier completed."),
                backend=self.backend,
                details={
                    "raw": result,
                    "available": True,
                },
            )
        except Exception as exc:
            return self._unavailable(f"Universal Verifier output schema is invalid: {error_text(exc)}")

    @staticmethod
    async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
        # The whole process group on POSIX (a descendant holding a pipe kept it open), then a drain that discards
        # and gives up after DRAIN_TIMEOUT_SECONDS: an uncapped communicate() hung on such a pipe and buffered it.
        await kill_and_drain(process, label="Universal Verifier command")

    def _unavailable(self, note: str) -> VerificationResult:
        return VerificationResult(
            passed=None,
            confidence=0.0,
            notes=note,
            backend=self.backend,
            details={"available": False},
        )
