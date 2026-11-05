"""Convergence run lifecycle: the attempt budget, timeouts, cancellation and restarts."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.harness import Budget, Postcondition, TaskContract
from app.harness.iterate import HarnessRunRecord, HarnessService
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


class _Orchestrator:
    async def run(self, **kwargs):
        return {"steps": [], "status": "done"}


class SimulatedTagTests(_Base):
    def _first_event(self, record) -> dict:
        trace_dir = Path(record.attempts[0].trace_path).parent
        return json.loads((trace_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()[0])

    async def test_a_mock_attempt_is_tagged_simulated(self) -> None:
        service = HarnessService(self.root)

        record = await service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE)

        self.assertIs(self._first_event(record)["payload"]["simulated"], True)

    async def test_an_orchestrated_attempt_is_not_tagged_simulated(self) -> None:
        service = HarnessService(self.root)

        record = await service.start_convergence(
            _contract(max_attempts=1), orchestrator=_Orchestrator(), session_id="sess-1"
        )

        self.assertIs(self._first_event(record)["payload"]["simulated"], False)


class _GatedVerifier:
    """Holds the first verification open until the test lets it go."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def verify(self, contract, trace):
        self.started.set()
        await self.release.wait()
        return VerificationResult(passed=True, confidence=1.0)


class CancelAndRestartTests(_Base):
    async def test_a_cancelled_run_is_marked_cancelled(self) -> None:
        verifier = _GatedVerifier()
        service = HarnessService(self.root, verifier=verifier)
        task = asyncio.create_task(service.start_convergence(_contract(), mock_final_observation=DONE))
        await verifier.started.wait()

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        (stored,) = service.store.list()
        self.assertEqual(stored.status, "cancelled")
        self.assertIn("cancelled", stored.notes[-1])

    async def test_a_crash_outside_the_attempts_marks_the_run_failed(self) -> None:
        service = HarnessService(self.root)

        class BrokenDetector:
            def __init__(self, **kwargs):
                pass

            def decide(self, results):
                raise RuntimeError("detector bug")

        with patch("app.harness.iterate.ConvergenceDetector", BrokenDetector):
            with self.assertRaises(RuntimeError):
                await service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE)

        (stored,) = service.store.list()
        self.assertEqual(stored.status, "failed")

    async def test_startup_sweep_interrupts_a_run_an_earlier_process_left_running(self) -> None:
        earlier = HarnessService(self.root)
        stale = HarnessRunRecord(
            id="stale000001",
            contract_id="c",
            contract_hash="h",
            status="running",
            created_at=time.time() - 60,
        )
        done = HarnessRunRecord(id="done0000001", contract_id="c", contract_hash="h", status="converged")
        earlier.store.save(stale)
        earlier.store.save(done)

        restarted = HarnessService(self.root)
        self.assertEqual(restarted.store.mark_stale_running(), 1)

        self.assertEqual(restarted.store.get("stale000001").status, "interrupted")
        self.assertIn("restart", restarted.store.get("stale000001").notes[-1])
        self.assertEqual(restarted.store.get("done0000001").status, "converged")

    async def test_startup_sweep_leaves_a_run_this_process_is_driving(self) -> None:
        verifier = _GatedVerifier()
        service = HarnessService(self.root, verifier=verifier)
        task = asyncio.create_task(service.start_convergence(_contract(), mock_final_observation=DONE))
        await verifier.started.wait()

        self.assertEqual(service.store.mark_stale_running(), 0)
        (live,) = service.store.list()
        self.assertEqual(live.status, "running")

        verifier.release.set()
        await task


if __name__ == "__main__":
    unittest.main()
