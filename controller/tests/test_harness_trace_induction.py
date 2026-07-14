from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.harness import (
    Budget,
    EvidenceRequirement,
    Postcondition,
    TaskContract,
    TraceEnvelope,
    TraceIntegrityError,
    TraceRecorder,
)
from app.harness.induce import SkillInducer
from app.harness.iterate import HarnessService
from app.harness.register import SkillStagingRegistry, mesh_identity_signer
from app.harness.run import _load_mock_observation
from app.harness.verifier.base import VerificationResult


class TraceRecorderTests(unittest.TestCase):
    def test_trace_recorder_hash_chains_events_and_indexes_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recorder = TraceRecorder(tmp, run_id="run-1", contract_hash="abc")
            first = recorder.append("model_decision", {"strategy": "dom_first"})
            second = recorder.append("action", {"action": "click", "selector": "#go"}, step_idx=1)
            recorder.append("screenshot", {"path": "shot.png"}, step_idx=1)

            trace = recorder.finalize(final_observation={"url": "https://example.com/done"})

            self.assertEqual(second.previous_hash, first.entry_hash)
            self.assertTrue(trace.verify_chain())
            self.assertIn("actions", trace.evidence)
            self.assertIn("screenshots", trace.evidence)
            self.assertIn("model_decisions", trace.evidence)
            self.assertTrue((Path(tmp) / "runs" / "run-1" / "trace.json").is_file())
            self.assertTrue((Path(tmp) / "trace-index.sqlite3").is_file())

    def test_trace_read_rejects_tampered_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recorder = TraceRecorder(tmp, run_id="run-2", contract_hash="abc")
            recorder.append("action", {"action": "click"}, step_idx=1)
            trace = recorder.finalize(final_observation={"url": "https://example.com"})
            path = Path(tmp) / "tampered.json"
            trace.write_json(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["events"][0]["payload"]["action"] = "type"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(TraceIntegrityError):
                TraceEnvelope.read_json(path)

    def test_trace_redacts_sensitive_payload_values(self) -> None:
        trace = TraceEnvelope.from_agent_result(
            contract_hash="abc",
            result={
                "steps": [
                    {
                        "status": "ok",
                        "decision": {"headers": {"Authorization": "Bearer secret-token"}},
                        "observation": {"text": "done", "cookie": "session=secret"},
                        "execution": {"api_key": "secret"},
                    }
                ],
                "final_session": {"current_url": "https://example.com"},
            },
        )
        payload_text = json.dumps(trace.model_dump(mode="json"))

        self.assertNotIn("secret-token", payload_text)
        self.assertNotIn("session=secret", payload_text)
        self.assertNotIn("api_key\": \"secret", payload_text)
        self.assertIn("[REDACTED]", payload_text)


class SkillInducerTests(unittest.TestCase):
    def test_inducer_stages_candidate_with_unsigned_envelope_payload(self) -> None:
        contract = _contract(max_attempts=1)
        trace = TraceEnvelope(
            run_id="trace-1",
            contract_hash=contract.hash(),
            final_observation={"url": "https://example.com/done", "text": "done"},
        )
        verification = VerificationResult(passed=True, confidence=1.0, backend="programmatic")
        with tempfile.TemporaryDirectory() as tmp:
            candidate = SkillInducer(tmp).induce(
                contract=contract,
                trace=trace,
                verification=verification,
                attempts=1,
                model_tiers={"explorer": "frontier", "executor": "cheap"},
            )

            self.assertEqual(candidate.envelope["kind"], "skill_candidate")
            self.assertEqual(candidate.envelope["candidate_id"], candidate.skill_id)
            self.assertEqual(candidate.envelope["metadata"]["validated_executor_model"], "cheap")
            self.assertTrue(candidate.skill_id.startswith("example-read-"))
            for name in ("SKILL.md", "helper.py", "test_skill.py", "provenance.json", "candidate.json"):
                self.assertTrue(Path(candidate.files[name]).is_file())
            provenance = json.loads(Path(candidate.files["provenance.json"]).read_text(encoding="utf-8"))
            self.assertEqual(provenance["contract"]["id"], contract.id)

    def test_inducer_slug_does_not_escape_staging_root(self) -> None:
        contract = _contract(max_attempts=1, contract_id="..")
        trace = TraceEnvelope(
            run_id="trace-slug",
            contract_hash=contract.hash(),
            final_observation={"url": "https://example.com/done", "text": "done"},
        )
        verification = VerificationResult(passed=True, confidence=1.0, backend="programmatic")
        with tempfile.TemporaryDirectory() as tmp:
            candidate = SkillInducer(tmp).induce(
                contract=contract,
                trace=trace,
                verification=verification,
                attempts=1,
            )
            candidate_path = Path(candidate.files["candidate.json"]).resolve()
            candidate_path.relative_to(Path(tmp).resolve())
            self.assertTrue(candidate.skill_id.startswith("skill-"))

    def test_inducer_hash_suffix_prevents_long_slug_collision(self) -> None:
        trace = TraceEnvelope(run_id="trace-collision", contract_hash="hash", final_observation={"url": "ok"})
        verification = VerificationResult(passed=True, confidence=1.0, backend="programmatic")
        with tempfile.TemporaryDirectory() as tmp:
            first = SkillInducer(tmp).induce(
                contract=_contract(max_attempts=1, contract_id=("a" * 100) + "x"),
                trace=trace,
                verification=verification,
                attempts=1,
            )
            second = SkillInducer(tmp).induce(
                contract=_contract(max_attempts=1, contract_id=("a" * 100) + "y"),
                trace=trace,
                verification=verification,
                attempts=1,
            )

            self.assertNotEqual(first.skill_id, second.skill_id)
            self.assertTrue(Path(first.files["candidate.json"]).is_file())
            self.assertTrue(Path(second.files["candidate.json"]).is_file())

    def test_staging_registry_rejects_candidate_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = SkillStagingRegistry(Path(tmp) / "staging")
            with self.assertRaises(KeyError):
                registry.get_candidate("../outside")

    def test_inducer_uses_mesh_signed_envelope_when_identity_is_available(self) -> None:
        from app.mesh.identity import NodeIdentity
        from app.mesh.models import PeerRecord, SignedEnvelope
        from app.mesh.transport import verify_envelope

        contract = _contract(max_attempts=1)
        trace = TraceEnvelope(run_id="trace-2", contract_hash=contract.hash(), final_observation={"url": "ok"})
        verification = VerificationResult(passed=True, confidence=1.0, backend="programmatic")
        with tempfile.TemporaryDirectory() as tmp:
            identity = NodeIdentity(Path(tmp) / "identity")
            candidate = SkillInducer(Path(tmp) / "staging", signer=mesh_identity_signer(identity)).induce(
                contract=contract,
                trace=trace,
                verification=verification,
                attempts=1,
            )

            envelope = SignedEnvelope.model_validate(candidate.envelope)
            peer = PeerRecord(node_id=identity.node_id, pubkey_b64=identity.pubkey_b64)
            payload = verify_envelope(envelope, peer)
            self.assertEqual(payload["kind"], "skill_candidate")
            self.assertTrue(payload["metadata"]["credential_free"])

    def test_mesh_signer_fails_closed_if_envelope_signing_fails(self) -> None:
        from app.mesh.identity import NodeIdentity

        with tempfile.TemporaryDirectory() as tmp:
            identity = NodeIdentity(Path(tmp) / "identity")
            with patch("app.mesh.transport.make_envelope", side_effect=RuntimeError("signing failed")):
                signer = mesh_identity_signer(identity)
                with self.assertRaisesRegex(RuntimeError, "signing failed"):
                    signer({"kind": "skill_candidate"})


class HarnessCliTests(unittest.TestCase):
    def test_load_mock_observation_accepts_inline_json_without_path_probe(self) -> None:
        args = argparse.Namespace(
            mock_final_observation='{"url": "https://example.com", "text": "done"}',
            mock_final_url="",
            mock_final_text="",
        )

        self.assertEqual(
            _load_mock_observation(args),
            {"url": "https://example.com", "text": "done"},
        )


class HarnessServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_convergence_stages_candidate_without_promotion(self) -> None:
        contract = _contract(max_attempts=1)
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)
            record = await service.start_convergence(
                contract,
                mock_final_observation={"url": "https://example.com/done", "text": "done"},
            )

            self.assertEqual(record.status, "converged")
            self.assertIsNotNone(record.candidate)
            status = service.get_status(record.id)
            self.assertEqual(status["status"], "converged")
            trace = service.get_trace(record.id)
            self.assertEqual(trace["final_observation"]["url"], "https://example.com/done")
            self.assertFalse((Path(tmp) / "runs" / record.id / "attempt-1" / "runs").exists())
            graduated = service.graduate(record.id)
            self.assertEqual(graduated["status"], "staged")
            self.assertIn("not automatic", graduated["message"])
            candidates = service.list_candidates()
            self.assertEqual(len(candidates), 1)
            self.assertEqual(service.get_candidate(record.candidate.skill_id)["skill_id"], record.candidate.skill_id)

    async def test_failed_mock_run_does_not_stage_candidate(self) -> None:
        contract = _contract(max_attempts=1)
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)
            record = await service.start_convergence(
                contract,
                mock_final_observation={"url": "https://example.com/not-done", "text": "still running"},
            )

            self.assertEqual(record.status, "unconverged")
            self.assertIsNone(record.candidate)
            with self.assertRaises(ValueError):
                service.graduate(record.id)

    async def test_caller_attempt_override_cannot_leave_exhausted_run_active(self) -> None:
        # Given: the caller asks for more attempts than the contract permits.
        contract = _contract(max_attempts=1)
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)

            # When: the only permitted attempt fails verification.
            record = await service.start_convergence(
                contract,
                mock_final_observation={"url": "https://example.com/not-done", "text": "still running"},
                max_attempts=3,
            )

            # Then: the persisted run is terminal because its effective budget is exhausted.
            self.assertEqual(record.status, "unconverged")
            self.assertFalse(record.decision.should_continue)
            self.assertEqual(record.decision.total_attempts, 1)

    async def test_attempt_exception_is_persisted_as_failed_run(self) -> None:
        contract = _contract(max_attempts=2)
        orchestrator = _FailingOrchestrator()
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)
            record = await service.start_convergence(
                contract,
                orchestrator=orchestrator,
                session_id="session-1",
            )

            self.assertEqual(record.status, "failed")
            self.assertEqual(len(record.attempts), 1)
            self.assertIsNone(record.attempts[0].verification.passed)
            trace = service.get_trace(record.id, attempt_index=1)
            self.assertEqual(trace["metadata"]["mode"], "error")
            self.assertEqual(trace["events"][0]["event_type"], "error")

    async def test_get_trace_rejects_out_of_range_attempt_index(self) -> None:
        contract = _contract(max_attempts=1)
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)
            record = await service.start_convergence(
                contract,
                mock_final_observation={"url": "https://example.com/done", "text": "done"},
            )

            with self.assertRaises(KeyError):
                service.get_trace(record.id, attempt_index=2)

    async def test_wall_clock_budget_stops_slow_attempt(self) -> None:
        contract = _contract(max_attempts=2, max_wall_seconds=1)
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)
            record = await service.start_convergence(
                contract,
                orchestrator=_SlowOrchestrator(),
                session_id="session-1",
            )

            self.assertEqual(record.status, "over_budget")
            self.assertEqual(len(record.attempts), 1)
            self.assertIn("max_wall_seconds", record.attempts[0].verification.notes)


class _FailingOrchestrator:
    async def run(self, **kwargs):
        raise RuntimeError("provider exploded with token=secret")


class _SlowOrchestrator:
    async def run(self, **kwargs):
        await asyncio.sleep(2)
        return {}


def _contract(
    *,
    max_attempts: int,
    contract_id: str = "example-read",
    max_wall_seconds: int = 300,
) -> TaskContract:
    return TaskContract(
        id=contract_id,
        goal="Open the example page and confirm done state.",
        postconditions=[
            Postcondition(kind="url_contains", value="example.com/done"),
            Postcondition(kind="text_contains", value="done"),
        ],
        evidence_required=[
            EvidenceRequirement(kind="trace"),
            EvidenceRequirement(kind="actions"),
        ],
        budget=Budget(max_attempts=max_attempts, max_steps=2, max_wall_seconds=max_wall_seconds),
    )
