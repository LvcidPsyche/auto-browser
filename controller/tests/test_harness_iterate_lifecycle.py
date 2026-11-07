"""Convergence run lifecycle: the attempt budget, timeouts, cancellation and restarts."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.audit import reset_current_operator, set_current_operator
from app.config import Settings
from app.harness import Budget, Postcondition, TaskContract
from app.harness.iterate import HarnessRunRecord, HarnessService
from app.harness.register import mesh_identity_signer, mesh_identity_verifier
from app.harness.trace import TraceIntegrityError
from app.harness.verifier.base import VerificationResult
from app.mesh.identity import NodeIdentity

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

    async def test_w4_12_a_previous_generations_run_stamped_after_this_start_is_interrupted(self) -> None:
        # A run counted as this process's when created at or after its start: a clock stepped back across the restart
        # left the earlier process's run "running" forever.
        earlier = HarnessService(self.root)
        restarted = HarnessService(self.root)
        ahead = HarnessRunRecord(
            id="ahead000001",
            contract_id="c",
            contract_hash="h",
            status="running",
            generation=earlier.store.generation,
            created_at=time.time() + 3600,
        )
        legacy = HarnessRunRecord(
            id="legacy00001", contract_id="c", contract_hash="h", status="running", created_at=time.time() + 3600
        )
        ours = HarnessRunRecord(
            id="ours0000001",
            contract_id="c",
            contract_hash="h",
            status="running",
            generation=restarted.store.generation,
            created_at=time.time() - 3600,
        )
        for record in (ahead, legacy, ours):
            earlier.store.save(record)

        self.assertNotEqual(earlier.store.generation, restarted.store.generation)
        self.assertEqual(restarted.store.mark_stale_running(), 2)

        self.assertEqual(restarted.store.get("ahead000001").status, "interrupted")
        self.assertEqual(restarted.store.get("legacy00001").status, "interrupted")
        self.assertEqual(restarted.store.get("ours0000001").status, "running")

    async def test_w4_12_a_run_records_the_generation_of_the_process_that_started_it(self) -> None:
        service = HarnessService(self.root)
        record = await service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE)

        self.assertEqual(service.store.get(record.id).generation, service.store.generation)

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


# How long the gated prune holds the staging lock unless the test lets it go first: a loop stuck behind the lock
# gets it back after this, so a regression fails instead of hanging.
PRUNE_HOLD_SECONDS = 2.0


class StagingLockTests(_Base):
    """R5C-12: placing a candidate takes the staging lock, which retention's prune holds (in a worker thread) from a
    candidate's age check through its removal. Staged on the event loop, a run that met a prune stalled the loop."""

    def _gated_prune(self, service: HarnessService) -> tuple[threading.Event, threading.Event]:
        """A prune holding the staging lock in its own thread: (the event that lets it go, the one set once it has)."""
        held, release, released = threading.Event(), threading.Event(), threading.Event()

        def prune() -> None:
            with service.store.staging_lock:
                held.set()
                release.wait(PRUNE_HOLD_SECONDS)
            released.set()

        thread = threading.Thread(target=prune, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        return release, released

    @staticmethod
    def _watch_place(service: HarnessService) -> threading.Event:
        """Set once the run first tries to place its candidate (it then waits for the staging lock)."""
        entered = threading.Event()
        try_place = service.inducer.try_place

        def watched(build_dir: Path, target_dir: Path) -> bool:
            entered.set()
            return try_place(build_dir, target_dir)

        service.inducer.try_place = watched  # type: ignore[method-assign]
        return entered

    async def test_r5c_12_the_event_loop_runs_while_a_prune_holds_the_staging_lock(self) -> None:
        service = HarnessService(self.root)
        release, released = self._gated_prune(service)
        entered = self._watch_place(service)
        run = asyncio.create_task(service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE))

        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
        self.assertFalse(released.is_set(), "the event loop waited for the prune's staging lock")
        release.set()
        record = await asyncio.wait_for(run, 10)

        self.assertEqual(record.status, "converged")
        self.assertTrue(Path(record.candidate.files["candidate.json"]).is_file())

    async def test_r5c_12_a_cancel_while_the_candidate_is_placed_waits_for_it_and_records_it(self) -> None:
        # The worker thread cannot be stopped: the cancel waits for it, so the run names the candidate it staged.
        service = HarnessService(self.root)
        release, _ = self._gated_prune(service)
        entered = self._watch_place(service)
        run = asyncio.create_task(service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE))
        self.assertTrue(await asyncio.to_thread(entered.wait, 5))

        run.cancel()
        await asyncio.sleep(0.05)
        self.assertFalse(run.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(run, 10)

        (stored,) = service.store.list()
        self.assertEqual(stored.status, "converged")
        self.assertTrue(Path(stored.candidate.files["candidate.json"]).is_file())

    async def test_a_candidate_read_on_the_event_loop_during_a_restage_finds_a_whole_candidate(self) -> None:
        # Review of c2 (Codex 2): placed in a worker thread, a re-stage's two renames (the staged candidate aside, the
        # new build into its place) ran between candidate reads on the event loop, which take no lock: a read between
        # them found no candidate, or the new files under the old candidate's signature.
        from app.harness import induce

        service = HarnessService(self.root)
        first = await service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE)
        skill_id = first.candidate.skill_id
        staged = Path(first.candidate.files["candidate.json"]).parent
        loop_thread = threading.get_ident()
        set_aside, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        real_replace = induce.os.replace

        def replace(src, dst) -> None:
            real_replace(src, dst)
            if Path(src) == staged:  # the staged candidate is set aside; the new one is not in its place yet
                set_aside.set()
                if threading.get_ident() != loop_thread:
                    resume.wait(PRUNE_HOLD_SECONDS)  # a read on the event loop runs here

        with patch.object(induce.os, "replace", replace):
            run = asyncio.create_task(service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE))
            self.assertTrue(await asyncio.to_thread(set_aside.wait, 5))
            try:
                read: object = service.get_candidate(skill_id)
            except (KeyError, PermissionError) as exc:
                read = exc
            finally:
                resume.set()
            record = await asyncio.wait_for(run, 10)

        self.assertIsInstance(read, dict, f"a read during the re-stage failed: {read!r}")
        self.assertEqual(read["skill_id"], skill_id)
        self.assertEqual(record.candidate.skill_id, skill_id)

    async def test_a_restage_removes_the_candidate_it_replaced_off_the_event_loop(self) -> None:
        # Review of c2's fix (Codex): placed on the event loop, a re-stage also removed the candidate it replaced
        # there (its aside), so a slow removal stalled every task even with the staging lock free.
        from app.harness import induce

        service = HarnessService(self.root)
        await service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE)
        removing, loop_ran = threading.Event(), threading.Event()
        self.addCleanup(loop_ran.set)
        seen: list[bool] = []
        real_rmtree = shutil.rmtree

        def rmtree(path, *args, **kwargs):
            if Path(path).name.endswith(".old"):
                removing.set()
                seen.append(loop_ran.wait(PRUNE_HOLD_SECONDS))  # set by a task on the event loop
            return real_rmtree(path, *args, **kwargs)

        async def on_the_loop() -> None:
            await asyncio.to_thread(removing.wait, PRUNE_HOLD_SECONDS)
            loop_ran.set()

        with patch.object(induce.shutil, "rmtree", rmtree):
            watcher = asyncio.create_task(on_the_loop())
            record = await service.start_convergence(
                _contract(max_attempts=1), mock_final_observation={**DONE, "text": "done again"}
            )
            await asyncio.wait_for(watcher, 10)

        self.assertEqual(seen, [True], "the event loop waited for the replaced candidate's removal")
        self.assertEqual(record.status, "converged")
        self.assertEqual([entry.name for entry in service.store.staging_root.iterdir()], [record.candidate.skill_id])


class StagingShutdownTests(unittest.TestCase):
    """Review of c2 (Codex 1, GLM 2): asyncio.run cancels every task left when its main returns, a run's staging task
    too. Placed by a worker thread that went on after that cancel, the candidate was staged while the run's record
    never named it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_a_shutdown_during_staging_leaves_no_candidate_the_runs_record_does_not_name(self) -> None:
        service = HarnessService(self.root)
        held, release = threading.Event(), threading.Event()

        def prune() -> None:
            with service.store.staging_lock:
                held.set()
                release.wait(PRUNE_HOLD_SECONDS)

        thread = threading.Thread(target=prune, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        built = threading.Event()
        build = service.inducer._build

        def watched_build(*args, **kwargs):
            try:
                return build(*args, **kwargs)
            finally:
                built.set()

        service.inducer._build = watched_build  # type: ignore[method-assign]

        async def main() -> None:
            # Left running when main returns: asyncio.run cancels it, and its staging task, as a shutdown does.
            asyncio.ensure_future(service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE))
            self.assertTrue(await asyncio.to_thread(built.wait, 5))
            await asyncio.sleep(0.05)  # the stage now waits for the prune's staging lock
            threading.Timer(0.2, release.set).start()

        asyncio.run(main())

        (stored,) = service.store.list()
        placed = sorted(entry.name for entry in service.store.staging_root.iterdir())
        if stored.candidate is None:
            self.assertEqual(placed, [], "a candidate was staged that the run's record does not name")
        else:
            self.assertTrue(Path(stored.candidate.files["candidate.json"]).is_file())
        # The next start finds the run converged with no candidate staged for it, and marks it failed.
        HarnessService(self.root).store.recover_interrupted_staging()
        (after,) = HarnessService(self.root).store.list()
        self.assertEqual(after.status, "failed" if stored.candidate is None else "converged")


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


class RetentionReadTests(_Base):
    """c2's "not done": retention's prune removed a candidate in a worker thread while candidate reads on the event
    loop took no lock, so a read met a candidate half removed, which a signed candidate reads as tampered with."""

    def _signed_service(self) -> HarnessService:
        identity = NodeIdentity(self.root / "identity")
        return HarnessService(
            self.root / "harness",
            signer=mesh_identity_signer(identity),
            envelope_verifier=mesh_identity_verifier(identity),
        )

    async def _aged_candidate(self, service: HarnessService) -> tuple[str, Path]:
        record = await service.start_convergence(_contract(max_attempts=1), mock_final_observation=DONE)
        directory = Path(record.candidate.files["candidate.json"]).parent
        for path in [*directory.iterdir(), directory]:
            _age(path, 40)
        return record.candidate.skill_id, directory

    async def test_a_candidate_read_while_retention_removes_it_is_not_found_not_tampered_with(self) -> None:
        from app.harness import iterate

        service = self._signed_service()
        skill_id, directory = await self._aged_candidate(service)
        removing, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        real_rmtree = shutil.rmtree

        def paused_rmtree(path, *args, **kwargs):
            if Path(path).parent.resolve() == directory.parent.resolve() and not removing.is_set():
                (Path(path) / "SKILL.md").unlink()  # the removal is under way: the reads below run here
                removing.set()
                resume.wait(PRUNE_HOLD_SECONDS)
            return real_rmtree(path, *args, **kwargs)

        with patch.object(iterate.shutil, "rmtree", paused_rmtree):
            pruning = asyncio.ensure_future(asyncio.to_thread(service.store.prune, 30))
            self.assertTrue(await asyncio.to_thread(removing.wait, 5))
            try:
                with self.assertRaises(KeyError):
                    service.get_candidate(skill_id)
                with self.assertNoLogs("app.harness.register", "WARNING"):
                    self.assertEqual(service.list_candidates(), [])
                drift = await service.check_drift(skill_id)
            finally:
                resume.set()
            self.assertEqual(await pruning, 1)

        self.assertEqual(drift["status"], "missing")
        self.assertFalse(directory.exists())

    async def test_retention_waits_for_a_candidate_read_in_progress(self) -> None:
        from app.harness.register import SkillStagingRegistry

        service = self._signed_service()
        skill_id, directory = await self._aged_candidate(service)
        prune = threading.Thread(target=service.store.prune, args=(30,))
        self.addCleanup(prune.join, 10)
        finished_during_the_read: list[bool] = []
        real_verify_files = SkillStagingRegistry._verify_files

        def verify_files(registry, candidate, signed_hashes, folder):
            # candidate.json is read; the files it signs are read next, and retention runs in between.
            if prune.ident is None:
                prune.start()
                prune.join(0.5)
                finished_during_the_read.append(not prune.is_alive())
            return real_verify_files(registry, candidate, signed_hashes, folder)

        with patch.object(SkillStagingRegistry, "_verify_files", verify_files):
            read = service.get_candidate(skill_id)
        prune.join(10)

        self.assertEqual(finished_during_the_read, [False], "retention removed the candidate while it was read")
        self.assertEqual(read["skill_id"], skill_id)
        self.assertFalse(directory.exists())


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

    async def test_w4_11_a_candidate_restaged_while_prune_waits_to_remove_it_survives(self) -> None:
        # Prune read the old candidate's age, then removed whatever was at that path: a re-stage that replaced it in
        # between lost the new candidate. Staging's replace now waits for prune's age check and its rename of the
        # candidate to a hidden name, which it removes from (since c3's A6; it removed the candidate in place before).
        import threading

        from app.harness import iterate

        service = HarnessService(self.root)
        record = await self._run(service)
        target = Path(record.candidate.files["candidate.json"]).parent
        _age(target / "candidate.json", 40)
        _age(target, 40)
        build = service.store.staging_root / ".tmp-restaged"
        shutil.copytree(target, build)
        (build / "candidate.json").write_text('{"fresh": true}', encoding="utf-8")
        real_replace = os.replace
        prune_thread = threading.get_ident()
        restage = threading.Thread(target=service.inducer.place, args=(build, target))

        def paused_replace(src, dst, *args, **kwargs):
            # Resolved (W10-12): the record's path is resolved, while prune walks the staging root as configured,
            # which differs under a symlinked or 8.3-form temp directory (C:/Users/RUNNER~1 on a Windows runner).
            pruning = threading.get_ident() == prune_thread
            if pruning and Path(src).resolve() == target.resolve() and restage.ident is None:
                restage.start()
                restage.join(0.5)  # the replace runs now unless prune holds the staging lock
            return real_replace(src, dst, *args, **kwargs)

        with patch.object(iterate.os, "replace", paused_replace):
            service.store.prune(30)
        self.assertIsNotNone(restage.ident, f"prune never set the aged candidate at {target} aside")
        restage.join(10)

        self.assertFalse(restage.is_alive())
        self.assertEqual((target / "candidate.json").read_text(encoding="utf-8"), '{"fresh": true}')

    async def test_w6_3_a_run_directory_is_removed_without_the_staging_lock(self) -> None:
        # Staging only replaces staged candidates, so a stage that met the prune of a large run directory waited for
        # its rmtree for nothing.
        from app.harness import iterate

        service = HarnessService(self.root)
        record = await self._run(service)
        run_dir = service.store.run_dir(record.id)
        _age(run_dir / "run.json", 40)
        _age(run_dir, 40)
        build = service.store.staging_root / ".tmp-other"
        build.mkdir()
        target = service.store.staging_root / "other-skill"
        real_rmtree = shutil.rmtree
        seen: list[tuple[bool, bool]] = []

        def gated_rmtree(path, *args, **kwargs):
            if Path(path) == run_dir:
                stage = threading.Thread(target=service.inducer.place, args=(build, target))
                stage.start()
                stage.join(5)  # returns at once unless prune holds the staging lock
                seen.append((service.store.staging_lock.locked(), stage.is_alive()))
                if stage.is_alive():
                    service.store.staging_lock.release()  # let the stage finish, so the test ends
                    stage.join(10)
                    service.store.staging_lock.acquire()
            return real_rmtree(path, *args, **kwargs)

        with patch.object(iterate.shutil, "rmtree", gated_rmtree):
            service.store.prune(30)

        self.assertEqual(seen, [(False, False)])
        self.assertFalse(run_dir.exists())
        self.assertTrue(target.is_dir())

    async def test_w4_11_staging_replaces_a_candidate_under_the_stores_staging_lock(self) -> None:
        from app.harness import induce

        service = HarnessService(self.root)
        self.assertIs(service.inducer.lock, service.store.staging_lock)
        held: list[bool] = []
        real_move = induce._move_into_place

        def move(build_dir, target_dir):
            held.append(service.store.staging_lock.locked())
            real_move(build_dir, target_dir)

        with patch.object(induce, "_move_into_place", move):
            await self._run(service)
            await self._run(service)  # the same contract: a re-stage over the first candidate

        self.assertEqual(held, [True, True])

    async def test_w4_11_listing_skips_a_run_removed_while_it_sorts(self) -> None:
        service = HarnessService(self.root)
        kept, removed = await self._run(service), await self._run(service)
        globbed = sorted(service.store.runs_root.glob("*/run.json"))
        shutil.rmtree(service.store.run_dir(removed.id))  # pruned between the glob and the sort

        with patch.object(Path, "glob", lambda self, pattern: iter(globbed)):
            listed = service.store.list()

        self.assertEqual([record.id for record in listed], [kept.id])

    async def test_w5_9_listing_skips_a_run_it_cannot_stat_with_a_warning(self) -> None:
        # Only FileNotFoundError was skipped, so one run directory the controller could not stat (PermissionError)
        # failed the whole list_runs. The glob is pinned to what it listed: on 3.11 its own existence check already
        # drops such an entry, but one whose permissions change between the glob and the stat reaches it.
        service = HarnessService(self.root)
        kept, unreadable = await self._run(service), await self._run(service)
        blocked = service.store.runs_root / unreadable.id / "run.json"
        globbed = sorted(service.store.runs_root.glob("*/run.json"))
        real_stat = Path.stat

        def stat(self, *args, **kwargs):
            if self.name == "run.json" and self.parent.name == unreadable.id:
                raise PermissionError(13, "Permission denied", str(self))
            return real_stat(self, *args, **kwargs)

        with (
            patch.object(Path, "glob", lambda self, pattern: iter(globbed)),
            patch.object(Path, "stat", stat),
            self.assertLogs("app.harness.iterate", "WARNING") as logs,
        ):
            listed = service.store.list()

        self.assertEqual([record.id for record in listed], [kept.id])
        self.assertTrue(any(str(blocked) in line for line in logs.output))

    async def test_w4_22_the_run_cap_check_runs_in_a_worker_thread(self) -> None:
        # Reading every stored run.json on the event loop stalled it at each start; it runs in a worker thread now,
        # holding no lock (W5-5).
        from app.harness import iterate

        service = HarnessService(self.root, max_runs_per_operator=5)
        real_to_thread = asyncio.to_thread
        offloaded: list = []

        async def to_thread(function, /, *args, **kwargs):
            offloaded.append(getattr(function, "__func__", function))
            return await real_to_thread(function, *args, **kwargs)

        with patch.object(iterate.asyncio, "to_thread", to_thread):
            await self._run(service)

        self.assertIn(iterate.HarnessRunStore._owned_runs, offloaded)
        self.assertNotIn(iterate.HarnessRunStore.begin_run, offloaded)

    def _gate_scan(self, service) -> tuple[threading.Event, threading.Event]:
        """Hold the stored-run scan in its worker thread until the test sets the second event."""
        started, go_on = threading.Event(), threading.Event()
        real = service.store._owned_runs

        def gated(owner):
            started.set()
            go_on.wait(10)
            return real(owner)

        patch.object(service.store, "_owned_runs", side_effect=gated).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(go_on.set)
        return started, go_on

    async def _until(self, event: threading.Event) -> None:
        self.assertTrue(await asyncio.to_thread(event.wait, 10), "the scan never started")

    async def test_w5_5_a_cancel_during_the_cap_scan_reserves_nothing(self) -> None:
        # The reservation came before the try that gives it back, so a cancel during the scan kept the place: with
        # a cap of 1 that operator could start no run until a restart.
        service = HarnessService(self.root, max_runs_per_operator=1)
        started, go_on = self._gate_scan(service)
        task = asyncio.create_task(self._run(service))
        await self._until(started)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        go_on.set()
        await asyncio.sleep(0.2)  # the worker thread finishes its scan

        self.assertEqual(service.store._active, {})
        patch.stopall()
        await self._run(service)
        self.assertEqual(len(service.store.list()), 1)

    async def test_w5_5_release_does_not_wait_for_a_cap_scan(self) -> None:
        # The scan held the store's threading lock, so release (on the event loop when a run ends) blocked the loop
        # until every run.json had been read.
        service = HarnessService(self.root, max_runs_per_operator=3)
        self.assertTrue(service.store.begin_run("0123456789ab", None))
        started, go_on = self._gate_scan(service)
        task = asyncio.create_task(self._run(service))
        await self._until(started)
        try:
            await asyncio.wait_for(asyncio.to_thread(service.store.release, "0123456789ab"), 2)
        finally:
            go_on.set()
            await task
        self.assertNotIn("0123456789ab", service.store._active)

    async def test_w5_5_two_admissions_at_once_under_a_cap_of_one_admit_one(self) -> None:
        service = HarnessService(self.root, max_runs_per_operator=1)
        started, go_on = self._gate_scan(service)
        first = asyncio.create_task(self._run(service))
        second = asyncio.create_task(self._run(service))
        await self._until(started)
        go_on.set()
        results = await asyncio.gather(first, second, return_exceptions=True)

        self.assertEqual(sum(isinstance(result, HarnessRunRecord) for result in results), 1)
        refused = [result for result in results if isinstance(result, ValueError)]
        self.assertEqual(len(refused), 1)
        self.assertIn("HARNESS_MAX_RUNS_PER_OPERATOR", str(refused[0]))
        self.assertEqual(len(service.store.list()), 1)

    async def test_w7_5_the_admission_lock_holds_the_cap_while_a_run_ends_during_another_scan(self) -> None:
        # The one window the lock covers, since reserve re-reads the starting runs: B's scan has listed the stored
        # runs (none) when A is admitted, runs and ends, so A is neither starting nor in B's count. Without the lock
        # both are admitted under a cap of one.
        service = HarnessService(self.root, max_runs_per_operator=1)
        started, go_on = threading.Event(), threading.Event()
        self.addCleanup(go_on.set)
        real = service.store._owned_runs
        scans = 0

        def gated(owner):
            nonlocal scans
            scans += 1
            result = real(owner)
            if scans == 1:  # B's scan only: its result is read before A's run.json exists
                started.set()
                go_on.wait(10)
            return result

        with patch.object(service.store, "_owned_runs", side_effect=gated):
            second = asyncio.create_task(self._run(service))
            await self._until(started)
            first = asyncio.create_task(self._run(service))
            # With the lock A waits for B's admission, so it cannot finish here; without it, A runs to its end.
            await asyncio.wait({first}, timeout=2)
            go_on.set()
            results = await asyncio.gather(second, first, return_exceptions=True)

        admitted = [isinstance(result, HarnessRunRecord) for result in results]
        self.assertEqual(admitted, [True, False], results)
        self.assertIsInstance(results[1], ValueError)
        self.assertEqual(len(service.store.list()), 1)

    async def test_zero_days_keeps_everything(self) -> None:
        service = HarnessService(self.root)
        record = await self._run(service)
        _age(service.store.run_dir(record.id) / "run.json", 400)
        _age(service.store.run_dir(record.id), 400)

        self.assertEqual(service.store.prune(0), 0)
        self.assertTrue(service.store.run_dir(record.id).exists())

    async def test_the_default_retention_keeps_a_400_day_old_run_and_candidate(self) -> None:
        # R-6: a default of 30 deleted every older run, and the only copy of each staged candidate, on the first
        # start after the upgrade. An upgrade must delete nothing.
        with patch.dict(os.environ, {}, clear=True):
            days = Settings(_env_file=None).harness_retention_days
        service = HarnessService(self.root)
        record = await self._run(service)
        candidate = Path(record.candidate.files["candidate.json"])
        for path in (service.store.run_dir(record.id) / "run.json", service.store.run_dir(record.id), candidate):
            _age(path, 400)
        _age(candidate.parent, 400)

        self.assertEqual(service.store.prune(days), 0)
        self.assertTrue(service.store.run_dir(record.id).exists())
        self.assertTrue(candidate.exists())

    async def test_a_candidate_left_aside_by_a_kill_mid_restage_is_put_back_at_startup(self) -> None:
        # R-18: re-staging renames the old candidate to .tmp-<hex>.old, then the new build into place; a kill between
        # the two left the candidate only in the hidden aside, and nothing recovered it.
        service = HarnessService(self.root)
        record = await self._run(service)
        skill_id = record.candidate.skill_id
        staging = service.store.staging_root
        os.replace(staging / skill_id, staging / ".tmp-0123456789ab.old")
        (staging / ".tmp-ba9876543210").mkdir()  # the new build, never placed

        restarted = HarnessService(self.root)
        restarted.store.recover_interrupted_staging()

        self.assertEqual(restarted.get_candidate(skill_id)["skill_id"], skill_id)
        self.assertEqual(sorted(path.name for path in staging.iterdir()), [skill_id])

    async def test_an_aside_whose_candidate_is_still_in_place_is_removed(self) -> None:
        service = HarnessService(self.root)
        record = await self._run(service)
        staging = service.store.staging_root
        shutil.copytree(staging / record.candidate.skill_id, staging / ".tmp-0123456789ab.old")

        HarnessService(self.root).store.recover_interrupted_staging()

        self.assertEqual(sorted(path.name for path in staging.iterdir()), [record.candidate.skill_id])

    async def test_an_aside_with_a_skill_id_that_is_not_a_plain_name_is_left_with_a_warning(self) -> None:
        service = HarnessService(self.root)
        aside = service.store.staging_root / ".tmp-0123456789ab.old"
        for skill_id in ("../escape", ".hidden", "a/b", 7):
            with self.subTest(skill_id=skill_id):
                aside.mkdir(exist_ok=True)
                (aside / "candidate.json").write_text(json.dumps({"skill_id": skill_id}), encoding="utf-8")

                with self.assertLogs("app.harness.iterate", "WARNING"):
                    HarnessService(self.root).store.recover_interrupted_staging()

                self.assertTrue(aside.exists())
                # Where '../escape' would land: beside the staging root, not under the harness root.
                self.assertFalse((service.store.staging_root.parent / "escape").exists())

    async def test_a_converged_run_left_without_its_candidate_is_marked_failed(self) -> None:
        service = HarnessService(self.root)
        record = await self._run(service)
        shutil.rmtree(service.store.staging_root / record.candidate.skill_id)  # killed before the build was in place
        record.candidate = None
        service.store.save(record)

        restarted = HarnessService(self.root)
        restarted.store.recover_interrupted_staging()

        stored = restarted.store.get(record.id)
        self.assertEqual(stored.status, "failed")
        self.assertIsNone(stored.candidate)
        self.assertTrue(any("no skill candidate" in note for note in stored.notes), stored.notes)
        self.assertFalse(any("was interrupted" in note for note in stored.notes), stored.notes)

    async def test_cb3_5_a_candidate_staged_before_the_kill_is_attached_to_its_converged_run(self) -> None:
        # A kill after the build was in place, before run.json was saved: the run read converged with no candidate,
        # and recovery marked it failed although its candidate was staged.
        service = HarnessService(self.root)
        record = await self._run(service)
        staged = record.candidate
        record.candidate = None
        service.store.save(record)

        restarted = HarnessService(self.root)
        restarted.store.recover_interrupted_staging()

        stored = restarted.store.get(record.id)
        self.assertEqual(stored.status, "converged")
        self.assertIsNotNone(stored.candidate)
        self.assertEqual(stored.candidate.skill_id, staged.skill_id)
        self.assertEqual(stored.candidate.trace_hash, staged.trace_hash)

    async def test_cb3_5_a_staged_candidate_of_another_trace_is_not_attached(self) -> None:
        service = HarnessService(self.root)
        record = await self._run(service)
        candidate_path = service.store.staging_root / record.candidate.skill_id / "candidate.json"
        data = json.loads(candidate_path.read_text(encoding="utf-8"))
        data["trace_hash"] = "0" * 64  # a later run of the same contract replaced it
        candidate_path.write_text(json.dumps(data), encoding="utf-8")
        trace_path = service.store.staging_root / record.candidate.skill_id / "trace.json"
        trace_path.unlink()
        record.candidate = None
        service.store.save(record)

        restarted = HarnessService(self.root)
        restarted.store.recover_interrupted_staging()

        stored = restarted.store.get(record.id)
        self.assertEqual(stored.status, "failed")
        self.assertIsNone(stored.candidate)

    async def test_cb3_6_an_aside_whose_candidate_json_is_gone_is_removed_once_its_target_exists(self) -> None:
        # A kill during the aside's rmtree after a finished swap: no candidate.json, so it was warned about at every
        # start and never removed (and prune no longer reclaims it by default).
        service = HarnessService(self.root)
        record = await self._run(service)
        staging = service.store.staging_root
        aside = staging / ".tmp-0123456789ab.old"
        shutil.copytree(staging / record.candidate.skill_id, aside)
        (aside / "candidate.json").unlink()

        HarnessService(self.root).store.recover_interrupted_staging()

        self.assertEqual(sorted(path.name for path in staging.iterdir()), [record.candidate.skill_id])

    async def test_w2_29_an_aside_without_candidate_json_holding_only_staged_files_is_removed(self) -> None:
        # Nothing in it can be restored without candidate.json, whether or not its target is there (W2-29: it was
        # left, and warned about at every start, while its target was missing).
        service = HarnessService(self.root)
        record = await self._run(service)
        staging = service.store.staging_root
        aside = staging / ".tmp-0123456789ab.old"
        os.replace(staging / record.candidate.skill_id, aside)
        (aside / "candidate.json").unlink()

        HarnessService(self.root).store.recover_interrupted_staging()

        self.assertFalse(aside.exists())

    async def test_w2_29_an_empty_aside_or_one_with_only_staged_names_is_removed(self) -> None:
        # A kill between rmtree's last unlink and its rmdir leaves it empty; on a filesystem that lists entries in
        # creation order, candidate.json (written last) goes last, so the naming files can be gone first.
        staging = HarnessService(self.root).store.staging_root
        empty, partial = staging / ".tmp-000000000001.old", staging / ".tmp-000000000002.old"
        empty.mkdir()
        partial.mkdir()
        for name in ("SKILL.md", "trace.json"):
            (partial / name).write_text("x", encoding="utf-8")

        HarnessService(self.root).store.recover_interrupted_staging()

        self.assertEqual(sorted(path.name for path in staging.iterdir()), [])

    async def test_w2_29_a_signed_envelope_names_the_asides_target_through_its_payload(self) -> None:
        # mesh_identity_signer's envelope keeps the id under payload.skill_id; the top-level lookup missed it. The
        # unknown entry keeps the staged-names rule out, so only the payload lookup can remove it.
        service = HarnessService(self.root)
        record = await self._run(service)
        staging = service.store.staging_root
        aside = staging / ".tmp-0123456789ab.old"
        aside.mkdir()
        signed = {"sender_node_id": "n", "payload": {"skill_id": record.candidate.skill_id}, "signature_b64": "s"}
        (aside / "envelope.json").write_text(json.dumps(signed), encoding="utf-8")
        (aside / "not-a-staged-file").write_text("x", encoding="utf-8")

        HarnessService(self.root).store.recover_interrupted_staging()

        self.assertFalse(aside.exists())
        self.assertTrue((staging / record.candidate.skill_id).exists())

    async def test_w2_29_an_aside_with_other_entries_and_nothing_naming_it_is_left_with_a_true_warning(self) -> None:
        staging = HarnessService(self.root).store.staging_root
        aside = staging / ".tmp-0123456789ab.old"
        aside.mkdir()
        (aside / "not-a-staged-file").write_text("x", encoding="utf-8")

        with self.assertLogs("app.harness.iterate", "WARNING") as logs:
            HarnessService(self.root).store.recover_interrupted_staging()

        self.assertTrue(aside.exists())
        self.assertIn("has no candidate.json", logs.output[0])
        self.assertNotIn("its candidate.json names", logs.output[0])

    async def test_cb3_4_startup_restores_an_aged_aside_before_the_prune(self) -> None:
        # _init_harness calls recover_interrupted_staging, then prune; nothing pinned either. The order is not
        # visible in what prune keeps (an aside and its restored candidate age alike), so the calls are recorded.
        from types import SimpleNamespace

        from app.harness.iterate import HarnessRunStore
        from app.startup.extensions import _init_harness

        service = HarnessService(self.root)
        record = await self._run(service)
        skill_id = record.candidate.skill_id
        staging = service.store.staging_root
        aside = staging / ".tmp-0123456789ab.old"
        os.replace(staging / skill_id, aside)
        _age(aside / "candidate.json", 10)
        _age(aside, 10)
        calls: list[str] = []
        real_recover, real_prune = HarnessRunStore.recover_interrupted_staging, HarnessRunStore.prune

        def recover(store):
            calls.append("recover")
            return real_recover(store)

        def prune(store, days):
            calls.append(f"prune({days:g})")
            return real_prune(store, days)

        settings = Settings(_env_file=None, HARNESS_ROOT=str(self.root), HARNESS_RETENTION_DAYS=30)
        app = SimpleNamespace(state=SimpleNamespace(settings=settings, mesh_identity=None, tool_gateway=None))
        with (
            patch.object(HarnessRunStore, "recover_interrupted_staging", recover),
            patch.object(HarnessRunStore, "prune", prune),
        ):
            _init_harness(app)

        self.assertIsNotNone(app.state.harness_service)
        self.assertEqual(calls, ["recover", "prune(30)"])
        self.assertEqual(sorted(path.name for path in staging.iterdir()), [skill_id])
        self.assertEqual(app.state.harness_service.get_candidate(skill_id)["skill_id"], skill_id)

    async def test_prune_leaves_a_run_this_process_is_driving(self) -> None:
        service = HarnessService(self.root)
        record = await self._run(service)
        _age(service.store.run_dir(record.id) / "run.json", 400)
        _age(service.store.run_dir(record.id), 400)
        self.assertTrue(service.store.begin_run(record.id, None))

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

    async def test_w9_7_an_entry_whose_type_cannot_be_read_is_logged_and_the_prune_goes_on(self) -> None:
        # Path.is_dir() raises for EACCES or EIO on Python 3.11, and the check sat before the per-entry try, so one
        # unreadable entry ended a prune whose docstring says it never raises.
        from unittest.mock import patch

        service = HarnessService(self.root)
        unreadable = service.store.staging_root / "unreadable"
        unreadable.mkdir()
        aged = []
        for name in ("oldskill", "otherskill"):
            staged = service.store.staging_root / name
            staged.mkdir()
            _age(staged, 40)
            aged.append(staged)
        real_is_dir = Path.is_dir

        def is_dir(path: Path) -> bool:
            if path == unreadable:
                raise PermissionError(13, "Permission denied", str(path))
            return real_is_dir(path)

        with patch.object(Path, "is_dir", is_dir), self.assertLogs("app.harness.iterate", "WARNING") as logs:
            self.assertEqual(service.store.prune(30), 2)
        self.assertTrue(any("unreadable" in line for line in logs.output), logs.output)
        self.assertEqual([path for path in aged if path.exists()], [])

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
        # A start that holds its place but has not written run.json yet is counted: the scan of the stored runs
        # holds no lock, admissions are serialised by HarnessService._admission, and reserve re-reads the starting
        # runs under the store's _active_lock, so two starts at once cannot both pass a cap of one.
        service = HarnessService(self.root, max_runs_per_operator=1)
        self.assertTrue(service.store.begin_run("0123456789ab", None, max_owned=1))

        with self.assertRaisesRegex(ValueError, "HARNESS_MAX_RUNS_PER_OPERATOR"):
            await self._run(service)

        service.store.release("0123456789ab")
        await self._run(service)
        self.assertEqual(len(service.store.list()), 1)

    async def test_a_run_record_that_is_not_a_json_object_does_not_block_new_runs(self) -> None:
        # A run.json holding JSON that is not an object ([] or null) raised AttributeError out of the cap count,
        # so every start failed until the file was removed.
        service = HarnessService(self.root, max_runs_per_operator=1)
        for run_id, text in (("aaaaaaaaaaaa", "[]"), ("bbbbbbbbbbbb", "null")):
            (service.store.runs_root / run_id).mkdir(parents=True)
            (service.store.runs_root / run_id / "run.json").write_text(text, encoding="utf-8")

        record = await self._run(service)

        self.assertTrue((service.store.run_dir(record.id) / "run.json").is_file())


if __name__ == "__main__":
    unittest.main()
