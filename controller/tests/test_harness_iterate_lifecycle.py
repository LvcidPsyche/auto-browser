"""Convergence run lifecycle: the attempt budget, timeouts, cancellation and restarts."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.audit import reset_current_operator, set_current_operator
from app.config import Settings
from app.harness import Budget, Postcondition, TaskContract
from app.harness.iterate import HarnessRunRecord, HarnessService
from app.harness.trace import TraceIntegrityError
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

    async def test_a_stale_run_that_cannot_be_saved_is_logged_and_the_sweep_goes_on(self) -> None:
        # One record the sweep could not rewrite raised out of startup and left the controller without a harness.
        earlier = HarnessService(self.root)
        for run_id in ("stale000001", "stale000002"):
            earlier.store.save(
                HarnessRunRecord(
                    id=run_id, contract_id="c", contract_hash="h", status="running", created_at=time.time() - 60
                )
            )
        restarted = HarnessService(self.root)
        save = restarted.store.save

        def failing_save(record: HarnessRunRecord) -> None:
            if record.id == "stale000001":
                raise OSError("disk full")
            save(record)

        with (
            patch.object(restarted.store, "save", side_effect=failing_save),
            self.assertLogs("app.harness.iterate", "WARNING") as logs,
        ):
            self.assertEqual(restarted.store.mark_stale_running(), 1)

        self.assertEqual(restarted.store.get("stale000002").status, "interrupted")
        self.assertIn("stale000001", "\n".join(logs.output))

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


class StagingFailureTests(_Base):
    def _staging_entries(self, service) -> list[str]:
        return sorted(path.name for path in service.store.staging_root.iterdir())

    async def test_a_signer_failure_fails_the_run_and_leaves_no_files(self) -> None:
        def broken_signer(payload):
            raise RuntimeError("key unavailable")

        service = HarnessService(self.root, signer=broken_signer)

        record = await service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE)

        self.assertEqual(record.status, "failed")
        self.assertIsNone(record.candidate)
        self.assertEqual(record.decision.status, "failed")
        self.assertIn("RuntimeError", record.decision.reason)
        self.assertEqual(service.store.get(record.id).status, "failed")
        self.assertEqual(self._staging_entries(service), [])

    async def test_a_second_run_of_the_same_contract_replaces_the_staged_candidate(self) -> None:
        service = HarnessService(self.root)

        first = await service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE)
        second = await service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE)

        self.assertEqual(first.candidate.skill_id, second.candidate.skill_id)
        self.assertEqual(self._staging_entries(service), [second.candidate.skill_id])
        self.assertTrue(Path(second.candidate.files["candidate.json"]).is_file())

    async def test_a_candidate_being_built_is_not_listed(self) -> None:
        service = HarnessService(self.root)
        record = await service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE)
        staged = service.store.staging_root / record.candidate.skill_id
        shutil.copytree(staged, service.store.staging_root / ".tmp-copy")

        self.assertEqual([item["skill_id"] for item in service.list_candidates()], [record.candidate.skill_id])


class TraceBindingTests(_Base):
    def _edit_observation(self, record) -> None:
        path = Path(record.attempts[0].trace_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["final_observation"] = {"url": "https://example.com/forged", "text": "done"}
        path.write_text(json.dumps(data), encoding="utf-8")

    async def test_get_trace_refuses_a_trace_edited_on_disk(self) -> None:
        service = HarnessService(self.root)
        record = await service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE)
        self.assertEqual(service.get_trace(record.id)["final_observation"], DONE)

        self._edit_observation(record)

        with self.assertRaises(TraceIntegrityError):
            service.get_trace(record.id)

    async def test_a_run_recorded_without_a_trace_hash_skips_the_check(self) -> None:
        service = HarnessService(self.root)
        record = await service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE)
        run_json = service.store.run_dir(record.id) / "run.json"
        data = json.loads(run_json.read_text(encoding="utf-8"))
        del data["attempts"][0]["trace_hash"]
        run_json.write_text(json.dumps(data), encoding="utf-8")
        self._edit_observation(record)

        self.assertEqual(service.get_trace(record.id)["final_observation"]["url"], "https://example.com/forged")


def _age(path: Path, days: float) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


class RetentionTests(_Base):
    async def _run(self, service) -> HarnessRunRecord:
        return await service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE)

    async def test_prune_removes_runs_and_candidates_past_the_age_and_keeps_newer_ones(self) -> None:
        service = HarnessService(self.root)
        old = await self._run(service)
        new = await self._run(service)
        _age(service.store.run_dir(old.id) / "run.json", 40)
        _age(service.store.run_dir(old.id), 40)
        (service.store.staging_root / ".tmp-leftover").mkdir()
        _age(service.store.staging_root / ".tmp-leftover", 40)

        removed = service.store.prune(30)

        self.assertEqual(removed, 2)
        self.assertFalse(service.store.run_dir(old.id).exists())
        self.assertTrue(service.store.run_dir(new.id).exists())
        self.assertFalse((service.store.staging_root / ".tmp-leftover").exists())
        self.assertTrue(Path(new.candidate.files["candidate.json"]).exists())

    async def test_zero_days_keeps_everything(self) -> None:
        service = HarnessService(self.root)
        record = await self._run(service)
        _age(service.store.run_dir(record.id) / "run.json", 400)
        _age(service.store.run_dir(record.id), 400)

        self.assertEqual(service.store.prune(0), 0)
        self.assertTrue(service.store.run_dir(record.id).exists())

    async def test_prune_leaves_a_run_this_process_is_driving(self) -> None:
        service = HarnessService(self.root)
        record = await self._run(service)
        _age(service.store.run_dir(record.id) / "run.json", 400)
        _age(service.store.run_dir(record.id), 400)
        service.store.mark_active(record.id)

        self.assertEqual(service.store.prune(30), 0)
        self.assertTrue(service.store.run_dir(record.id).exists())

    async def test_prune_goes_on_to_the_staging_area_when_the_runs_folder_cannot_be_listed(self) -> None:
        # The listing sat outside prune's error handling, so a missing or unreadable runs folder raised out of a
        # sweep whose docstring says it never raises.
        service = HarnessService(self.root)
        staged = service.store.staging_root / "oldskill"
        staged.mkdir()
        _age(staged, 40)
        shutil.rmtree(service.store.runs_root)

        with self.assertLogs("app.harness.iterate", "WARNING"):
            self.assertEqual(service.store.prune(30), 1)
        self.assertFalse(staged.exists())

    async def test_the_per_operator_cap_refuses_a_run_past_it_and_counts_each_operator_apart(self) -> None:
        service = HarnessService(self.root, max_runs_per_operator=2)
        token = set_current_operator("alice", source="token")
        try:
            await self._run(service)
            await self._run(service)
            with self.assertRaisesRegex(ValueError, "HARNESS_MAX_RUNS_PER_OPERATOR"):
                await self._run(service)
        finally:
            reset_current_operator(token)

        token = set_current_operator("bob", source="token")
        try:
            await self._run(service)
        finally:
            reset_current_operator(token)
        # The unowned bucket has its own count too.
        await self._run(service)
        self.assertEqual(len(service.store.list(limit=50)), 4)

    async def test_no_cap_by_default(self) -> None:
        service = HarnessService(self.root)
        for _ in range(3):
            await self._run(service)
        self.assertEqual(len(service.store.list()), 3)

    def test_the_setting_defaults_to_no_cap_so_an_upgrade_refuses_no_one(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Settings().harness_max_runs_per_operator, 0)

    async def test_a_run_still_starting_counts_toward_the_cap(self) -> None:
        # The count and the mark are one step under the store's lock: a start that holds its place but has not
        # written run.json yet is counted, so two starts at once cannot both pass a cap of one.
        service = HarnessService(self.root, max_runs_per_operator=1)
        self.assertTrue(service.store.begin_run("0123456789ab", None, max_owned=1))

        with self.assertRaisesRegex(ValueError, "HARNESS_MAX_RUNS_PER_OPERATOR"):
            await self._run(service)

        service.store.release("0123456789ab")
        await self._run(service)
        self.assertEqual(len(service.store.list()), 1)


if __name__ == "__main__":
    unittest.main()
