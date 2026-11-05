"""A staged skill stores a scrubbed trace, so the drift monitor must expect scrubbed text too."""

from __future__ import annotations

import tempfile
import unittest

from app.harness import Budget, Postcondition, TaskContract
from app.harness.induce import SkillInducer
from app.harness.iterate import HarnessService
from app.harness.trace import TraceEnvelope
from app.harness.verifier.base import VerificationResult

EMAIL = "alice@example.com"


def _contract(kind: str = "dom_contains") -> TaskContract:
    return TaskContract(
        id="drift-pii",
        goal="Open the page.",
        postconditions=[Postcondition(kind=kind, value=EMAIL)],
        budget=Budget(max_attempts=1, max_steps=2),
    )


def _observation() -> dict:
    return {
        "url": "https://example.com/done",
        "text": "done",
        "accessibility_outline": {"headings": [f"Signed in as {EMAIL}"]},
    }


class DriftOnRedactedTraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_fresh_skill_whose_live_run_passed_on_an_email_is_not_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)
            record = await service.start_convergence(_contract(), mock_final_observation=_observation())

            drift = await service.drift_monitor.check_candidate(record.candidate.skill_id)

            self.assertEqual(record.status, "converged")
            self.assertEqual(drift.status, "healthy", drift.notes)

    async def test_a_text_contains_on_an_email_is_not_degraded_either(self) -> None:
        observation = {**_observation(), "text": f"Signed in as {EMAIL}"}
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)
            record = await service.start_convergence(_contract("text_contains"), mock_final_observation=observation)

            drift = await service.drift_monitor.check_candidate(record.candidate.skill_id)

            self.assertEqual(drift.status, "healthy", drift.notes)

    async def test_a_skill_whose_page_really_changed_still_degrades(self) -> None:
        # The stored trace no longer shows the expected text at all, so scrubbing the expectation must not save it.
        contract = _contract()
        trace = TraceEnvelope(
            contract_hash=contract.hash(),
            final_observation={"url": "https://example.com/done", "text": "done", "accessibility_outline": {}},
        )
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)
            candidate = SkillInducer(service.store.staging_root).induce(
                contract=contract,
                trace=trace,
                verification=VerificationResult(passed=True, confidence=1.0),
                attempts=1,
            )

            drift = await service.drift_monitor.check_candidate(candidate.skill_id)

            self.assertEqual(drift.status, "degraded")
            self.assertTrue(drift.verification.failed_postconditions)


if __name__ == "__main__":
    unittest.main()
