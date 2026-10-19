from __future__ import annotations

import asyncio
import json
from shutil import which
from typing import Any

from ..contracts import TaskContract
from .base import VerificationResult


class UniversalVerifierAdapter:
    """Explicit dependency-checking adapter for external Universal Verifier backends."""

    backend = "uv"

    def __init__(self, command: list[str] | None = None, timeout_seconds: float = 60.0):
        self.command = command
        self.timeout_seconds = timeout_seconds

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
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(payload).encode("utf-8")),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            await self._kill_and_reap(process)
            return self._unavailable("Universal Verifier command timed out.")
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
            return self._unavailable(f"Universal Verifier output schema is invalid: {exc}")

    @staticmethod
    async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
        try:
            process.kill()
        except ProcessLookupError:
            pass  # exited between the timeout and the kill
        await process.communicate()

    def _unavailable(self, note: str) -> VerificationResult:
        return VerificationResult(
            passed=None,
            confidence=0.0,
            notes=note,
            backend=self.backend,
            details={"available": False},
        )
