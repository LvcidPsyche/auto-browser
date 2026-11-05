"""Convergence run lifecycle: the attempt budget, timeouts, cancellation and restarts."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.harness import Budget, Postcondition, TaskContract
from app.harness.iterate import HarnessService
from app.harness.verifier.base import VerificationResult

DONE = {"url": "https://example.com/done", "text": "done"}


def _contract(*, max_attempts: int = 5, max_wall_seconds: int = 300) -> TaskContract:
    return TaskContract(
        id="lifecycle",
        goal="Open the example page and confirm done state.",
        postconditions=[Postcondition(kind="url_contains", value="example.com/done")],
        budget=Budget(max_attempts=max_attempts, max_steps=2, max_wall_seconds=max_wall_seconds),
    )


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


class AttemptOverrideTests(_Base):
    async def test_override_below_the_threshold_can_still_converge(self) -> None:
        service = HarnessService(self.root)

        record = await service.start_convergence(_contract(max_attempts=5), mock_final_observation=DONE, max_attempts=1)

        self.assertEqual(record.status, "converged")
        self.assertEqual(len(record.attempts), 1)


class _RaisingVerifier:
    def __init__(self, error: BaseException):
        self.error = error

    async def verify(self, contract, trace):
        raise self.error


class _SlowVerifier:
    async def verify(self, contract, trace):
        await asyncio.sleep(30)
        return VerificationResult(passed=True, confidence=1.0)


class TimeoutClassificationTests(_Base):
    async def test_a_timeout_inside_the_verifier_with_budget_left_is_a_failed_attempt(self) -> None:
        service = HarnessService(self.root, verifier=_RaisingVerifier(TimeoutError("uv command timed out")))

        record = await service.start_convergence(_contract(), mock_final_observation=DONE)

        self.assertEqual(record.status, "failed")
        self.assertEqual(record.decision.status, "failed")
        self.assertEqual(record.attempts[-1].verification.notes, "Harness attempt failed: TimeoutError")

    async def test_running_past_max_wall_seconds_is_still_over_budget(self) -> None:
        service = HarnessService(self.root, verifier=_SlowVerifier())

        record = await service.start_convergence(_contract(max_wall_seconds=1), mock_final_observation=DONE)

        self.assertEqual(record.status, "over_budget")
        self.assertEqual(record.decision.reason, "max_wall_seconds budget exhausted")


if __name__ == "__main__":
    unittest.main()
