"""Verification results copy raw observation values (the URL), so they are scrubbed wherever they are stored."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.harness import Budget, Postcondition, TaskContract
from app.harness.drift import DriftCheckResult
from app.harness.iterate import HarnessService
from app.harness.verifier.base import VerificationResult

URL_TOKEN = "tok_live_ABC"
TOKEN_URL = f"https://example.com/done?access_token={URL_TOKEN}"


def _contract() -> TaskContract:
    return TaskContract(
        id="verification-scrub",
        goal="Open the page.",
        postconditions=[Postcondition(kind="url_contains", value="example.com/done")],
        budget=Budget(max_attempts=1, max_steps=2),
    )


class VerificationScrubTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_url_token_in_the_verification_details_reaches_no_stored_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = await HarnessService(tmp).start_convergence(
                _contract(), mock_final_observation={"url": TOKEN_URL, "text": "done"}
            )
            await HarnessService(tmp).check_all_drifts()

            self.assertEqual(record.status, "converged")
            stored = [path for path in Path(tmp).rglob("*") if path.is_file()]
            names = {path.name for path in stored}
            for expected in ("run.json", "candidate.json", "envelope.json", "provenance.json", "drift.json"):
                self.assertIn(expected, names)
            for path in stored:
                self.assertNotIn(URL_TOKEN.encode(), path.read_bytes(), f"{path.name} holds the token")

    async def test_a_failing_verifier_result_with_a_token_does_not_reach_strategy_md(self) -> None:
        class LeakyVerifier:
            async def verify(self, contract, trace) -> VerificationResult:
                return VerificationResult(
                    passed=False,
                    confidence=1.0,
                    failed_postconditions=[f"expected the page, saw {TOKEN_URL}"],
                    missing_evidence=[TOKEN_URL],
                )

        with tempfile.TemporaryDirectory() as tmp:
            record = await HarnessService(tmp, verifier=LeakyVerifier()).start_convergence(
                _contract(), mock_final_observation={"url": TOKEN_URL, "text": "done"}
            )

            strategy = Path(record.strategy_path).read_bytes()
            self.assertIn(b"expected the page", strategy)
            for path in Path(tmp).rglob("*"):
                if path.is_file():
                    self.assertNotIn(URL_TOKEN.encode(), path.read_bytes(), f"{path.name} holds the token")
            self.assertNotIn(URL_TOKEN, record.model_dump_json())

    def test_the_drift_result_scrubs_a_raw_verification_itself(self) -> None:
        raw = VerificationResult(passed=True, confidence=1.0, details={"url": TOKEN_URL})

        result = DriftCheckResult(skill_id="s", status="healthy", verification=raw)

        self.assertNotIn(URL_TOKEN, result.model_dump_json())

    async def test_the_drift_result_notes_carry_the_verifiers_notes_scrubbed(self) -> None:
        class NotingVerifier:
            async def verify(self, contract, trace) -> VerificationResult:
                return VerificationResult(passed=True, confidence=1.0, notes=f"checked {TOKEN_URL}")

        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp, verifier=NotingVerifier())
            record = await service.start_convergence(_contract(), mock_final_observation={"url": TOKEN_URL})

            drift = await service.check_drift(record.candidate.skill_id)

            self.assertTrue(any(note.startswith("checked https://example.com/done") for note in drift["notes"]))
            self.assertNotIn(URL_TOKEN, json.dumps(drift))
            stored = Path(service.registry.candidate_dir(record.candidate.skill_id)) / "drift.json"
            self.assertNotIn(URL_TOKEN.encode(), stored.read_bytes())

    async def test_a_stored_run_is_read_back_without_scrubbing_it_again(self) -> None:
        # Scrubbed once, when its attempt was made: a scrubber that fails later does not drop the run from the list.
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)
            record = await service.start_convergence(_contract(), mock_final_observation={"url": TOKEN_URL})

            with patch.object(VerificationResult, "scrubbed", side_effect=ValueError("unknown PII_SCRUB_PATTERNS")):
                listed = service.list_runs()
                status = service.get_status(record.id)

            self.assertEqual([run["id"] for run in listed], [record.id])
            self.assertEqual(status["attempts"], listed[0]["attempts"])
            self.assertNotIn(URL_TOKEN, json.dumps(listed))

    def test_scrubbed_keeps_the_verdict_and_drops_the_token(self) -> None:
        result = VerificationResult(
            passed=True,
            confidence=1.0,
            satisfied_postconditions=["postcondition[0]:url_contains:example.com/done"],
            details={"url": TOKEN_URL, "evidence": ["trace"]},
        )

        scrubbed = result.scrubbed()

        self.assertIs(scrubbed.passed, True)
        self.assertEqual(scrubbed.satisfied_postconditions, result.satisfied_postconditions)
        self.assertNotIn(URL_TOKEN, scrubbed.model_dump_json())
        self.assertIn(URL_TOKEN, result.model_dump_json())  # the original is untouched
