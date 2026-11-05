"""Harness traces kept typed secrets and URL tokens, and induced skills claimed to be credential free."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.harness.trace import TraceEnvelope, TraceRecorder

SECRET_TEXT = "Sup3rS3cret-Passw0rd!"
URL_TOKEN = "tok_live_ABC"
EMAIL = "alice@example.com"


def _trace_text(trace: TraceEnvelope) -> str:
    return json.dumps(trace.model_dump(mode="json"))


def _agent_result(**step: object) -> dict:
    return {"steps": [{"status": "ok", **step}]}


class TraceRedactionTests(unittest.TestCase):
    def test_typed_text_of_a_sensitive_action_is_redacted(self) -> None:
        trace = TraceEnvelope.from_agent_result(
            contract_hash="h",
            result=_agent_result(
                decision={"action": "type", "selector": "#pw", "text": SECRET_TEXT, "sensitive": True},
                execution={"target": {"text_preview": SECRET_TEXT, "text_redacted": True}},
            ),
        )

        self.assertNotIn(SECRET_TEXT, _trace_text(trace))
        self.assertEqual(trace.events[0].payload["decision"]["selector"], "#pw")

    def test_typed_text_is_redacted_when_the_field_was_found_sensitive_at_run_time(self) -> None:
        # The agent did not mark the step sensitive; the controller saw a password field and said so.
        trace = TraceEnvelope.from_agent_result(
            contract_hash="h",
            result=_agent_result(
                decision={"action": "type", "selector": "#pw", "text": SECRET_TEXT},
                execution={"target": {"selector": "#pw", "text_redacted": True}},
            ),
        )

        self.assertNotIn(SECRET_TEXT, _trace_text(trace))

    def test_ordinary_typed_text_is_kept(self) -> None:
        trace = TraceEnvelope.from_agent_result(
            contract_hash="h",
            result=_agent_result(decision={"action": "type", "selector": "#q", "text": "blue shoes"}),
        )

        self.assertEqual(trace.events[0].payload["decision"]["text"], "blue shoes")

    def test_url_tokens_are_scrubbed_from_the_observation_and_the_final_observation(self) -> None:
        url = f"https://app.example.com/cb?access_token={URL_TOKEN}#id_token={URL_TOKEN}"
        trace = TraceEnvelope.from_agent_result(
            contract_hash="h",
            result={**_agent_result(observation={"url": url, "title": "Login"}), "final_session": {"current_url": url}},
        )

        self.assertNotIn(URL_TOKEN, _trace_text(trace))
        self.assertTrue(trace.final_observation["url"].startswith("https://app.example.com/cb"))

    def test_free_text_is_scrubbed_by_the_pii_scrubber(self) -> None:
        trace = TraceEnvelope.from_agent_result(
            contract_hash="h",
            result=_agent_result(observation={"text_excerpt": f"Signed in as {EMAIL}"}),
        )

        self.assertNotIn(EMAIL, _trace_text(trace))

    def test_a_recorded_event_is_scrubbed_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recorder = TraceRecorder(tmp, run_id="r", contract_hash="h")
            recorder.append("action", {"action": "type", "text": SECRET_TEXT, "sensitive": True}, step_idx=1)
            recorder.append("action", {"note": f"mail {EMAIL}"}, step_idx=2)

            trace = recorder.finalize(final_observation={"url": f"https://x.test/?access_token={URL_TOKEN}"})

            self.assertNotIn(SECRET_TEXT, _trace_text(trace))
            self.assertNotIn(EMAIL, _trace_text(trace))
            self.assertNotIn(URL_TOKEN, _trace_text(trace))
            self.assertNotIn(SECRET_TEXT, (Path(tmp) / "runs" / "r" / "trace.jsonl").read_text(encoding="utf-8"))
            self.assertTrue(trace.verify_chain())

    def test_a_staged_candidate_makes_no_credential_free_claim(self) -> None:
        from app.harness.induce import SkillInducer
        from app.harness.verifier.base import VerificationResult
        from tests.test_harness_trace_induction import _contract

        contract = _contract(max_attempts=1)
        trace = TraceEnvelope(run_id="t", contract_hash=contract.hash(), final_observation={"url": "ok"})
        with tempfile.TemporaryDirectory() as tmp:
            candidate = SkillInducer(Path(tmp)).induce(
                contract=contract,
                trace=trace,
                verification=VerificationResult(passed=True, confidence=1.0, backend="programmatic"),
                attempts=1,
            )

        self.assertNotIn("credential_free", candidate.envelope["metadata"])


if __name__ == "__main__":
    unittest.main()
