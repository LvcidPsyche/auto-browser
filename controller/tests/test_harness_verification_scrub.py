"""Verification results copy raw observation values (the URL), so they are scrubbed wherever they are stored."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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
