"""Harness traces kept typed secrets and URL tokens, and induced skills claimed to be credential free."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.harness import Budget, ForbiddenState, Postcondition, TaskContract
from app.harness.iterate import HarnessService
from app.harness.trace import TraceEnvelope, TraceRecorder

SECRET_TEXT = "Sup3rS3cret-Passw0rd!"
URL_TOKEN = "tok_live_ABC"
# The local part of an encoded email in a query: only the URL scrubber catches it.
PLAIN_TOKEN = "Zq81hdk3"
EMAIL = "alice@example.com"


def _trace_text(trace: TraceEnvelope) -> str:
    """The trace as it is stored."""
    return json.dumps(trace.scrubbed().model_dump(mode="json"))


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
        self.assertTrue(trace.scrubbed().final_observation["url"].startswith("https://app.example.com/cb"))

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


class StoredUrlTests(unittest.TestCase):
    def _stored(self, **fields: object) -> TraceEnvelope:
        return TraceEnvelope(contract_hash="h", **fields).scrubbed()

    def test_an_upper_case_scheme_is_still_a_url(self) -> None:
        stored = self._stored(final_observation={"url": f"HTTPS://x.test/cb?email={PLAIN_TOKEN}%40a.com"})

        self.assertNotIn(PLAIN_TOKEN, json.dumps(stored.final_observation))

    def test_a_url_inside_a_sentence_is_scrubbed_and_the_text_around_it_kept(self) -> None:
        text = f"Redirected to https://x.test/cb?email={PLAIN_TOKEN}%40a.com after login"
        stored = self._stored(final_observation={"text": text})

        out = stored.final_observation["text"]
        self.assertNotIn(PLAIN_TOKEN, out)
        self.assertTrue(out.startswith("Redirected to https://x.test/cb"))
        self.assertTrue(out.endswith("after login"))

    def test_a_url_in_extracted_data_loses_its_token_but_other_text_is_kept(self) -> None:
        stored = self._stored(
            extracted_data={
                "link": f"https://x.test/cb?email={PLAIN_TOKEN}%40a.com",
                "note": f"mail {EMAIL} via https://x.test/cb?email={PLAIN_TOKEN}%40a.com",
            }
        )

        dumped = json.dumps(stored.extracted_data)
        self.assertNotIn(PLAIN_TOKEN, dumped)
        self.assertIn(EMAIL, stored.extracted_data["note"])

    def test_the_file_on_disk_holds_no_url_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = TraceEnvelope(
                contract_hash="h", extracted_data={"link": f"https://x.test/?email={PLAIN_TOKEN}%40a.com"}
            )
            path = envelope.write_json(Path(tmp) / "trace.json")

            self.assertNotIn(PLAIN_TOKEN, path.read_text(encoding="utf-8"))


def _contract(**extra: object) -> TaskContract:
    return TaskContract(
        id="pii-verdict",
        goal="Open the page.",
        postconditions=[Postcondition(kind="url_contains", value="example.com/done")],
        budget=Budget(max_attempts=1, max_steps=2),
        **extra,
    )


def _observation() -> dict:
    return {
        "url": "https://example.com/done",
        "text": "done",
        "accessibility_outline": {"headings": [f"Signed in as {EMAIL}"]},
    }


class VerificationSeesTheOriginalTests(unittest.IsolatedAsyncioTestCase):
    """Scrubbing is for what is stored; the verdict must not depend on it."""

    async def test_a_forbidden_state_on_an_email_still_fails_the_run(self) -> None:
        contract = _contract(forbidden_states=[ForbiddenState(kind="text_contains", value=EMAIL)])
        with tempfile.TemporaryDirectory() as tmp:
            record = await HarnessService(tmp).start_convergence(contract, mock_final_observation=_observation())

        self.assertEqual(record.status, "unconverged")
        self.assertTrue(record.attempts[0].verification.forbidden_state_hits)

    async def test_a_required_dom_contains_on_an_email_still_passes(self) -> None:
        contract = TaskContract(
            id="pii-dom",
            goal="Open the page.",
            postconditions=[Postcondition(kind="dom_contains", value=EMAIL)],
            budget=Budget(max_attempts=1, max_steps=2),
        )
        with tempfile.TemporaryDirectory() as tmp:
            record = await HarnessService(tmp).start_convergence(contract, mock_final_observation=_observation())

        self.assertEqual(record.status, "converged")

    async def test_what_is_stored_holds_no_email(self) -> None:
        contract = _contract()
        with tempfile.TemporaryDirectory() as tmp:
            record = await HarnessService(tmp).start_convergence(contract, mock_final_observation=_observation())

            self.assertEqual(record.status, "converged")
            stored = [path for path in Path(tmp).rglob("*") if path.is_file() and path.suffix != ".sqlite3"]
            self.assertTrue(any(path.name == "trace.json" for path in stored))
            self.assertIsNotNone(record.candidate)
            self.assertTrue(any(path.name == "candidate.json" for path in stored))
            for path in stored:
                self.assertNotIn(EMAIL, path.read_text(encoding="utf-8"), f"{path.name} holds the email")
            trace = (Path(tmp) / "runs" / record.id / "attempt-1" / "trace.json").read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", trace)


if __name__ == "__main__":
    unittest.main()
