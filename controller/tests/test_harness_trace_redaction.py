"""Harness traces kept typed secrets and URL tokens, and induced skills claimed to be credential free."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.harness import Budget, ForbiddenState, Postcondition, TaskContract
from app.harness.iterate import HarnessService
from app.harness.trace import _URL_PATTERN, TraceEnvelope, TraceRecorder

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


def _event(trace: TraceEnvelope, event_type: str):
    return next(event for event in trace.events if event.event_type == event_type)


class PageDataKeepsItsValuesTests(unittest.TestCase):
    """`value`, `label` and `username` are hidden only in an action's own payload, not in what the page held."""

    ROW = {"label": "Plan", "value": "Pro", "username": "team-admin", "sensitive": True}

    def test_a_sensitive_flag_in_page_data_does_not_hide_its_neighbours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recorder = TraceRecorder(tmp, run_id="r1", contract_hash="h")
            trace = recorder.finalize(final_observation={"rows": [self.ROW]}, extracted_data={"row": self.ROW})

        stored = trace.scrubbed()
        self.assertEqual(stored.final_observation["rows"][0]["value"], "Pro")
        self.assertEqual(stored.extracted_data["row"]["label"], "Plan")
        self.assertEqual(stored.extracted_data["row"]["username"], "team-admin")

    def test_typed_text_beside_a_sensitive_flag_in_page_data_is_still_hidden(self) -> None:
        # Only `value`, `label` and `username` moved to action payloads: `text` and `text_preview` in a dict that
        # carries a sensitive flag stay hidden wherever the dict is, as they were before.
        typed = "ordinary typed words"
        for flag in ("sensitive", "text_redacted"):
            row = {"text": typed, "text_preview": typed, "value": "Pro", flag: True}
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as tmp:
                recorder = TraceRecorder(tmp, run_id="r1", contract_hash="h")
                trace = recorder.finalize(final_observation={"rows": [row]}, extracted_data={"row": row})

                stored = trace.scrubbed()
                self.assertNotIn(typed, json.dumps(stored.model_dump(mode="json")))
                self.assertEqual(stored.final_observation["rows"][0]["value"], "Pro")
                self.assertEqual(stored.extracted_data["row"]["value"], "Pro")

    def test_an_agent_result_final_observation_keeps_page_data(self) -> None:
        result = {"steps": [{"status": "ok", "observation": {"url": "https://example.com/", "rows": [self.ROW]}}]}

        trace = TraceEnvelope.from_agent_result(contract_hash="h", result=result)

        self.assertEqual(trace.final_observation["rows"][0]["value"], "Pro")

    def test_an_action_payload_still_hides_what_it_typed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recorder = TraceRecorder(tmp, run_id="r1", contract_hash="h")
            event = recorder.append("action", {"action": "type", "value": SECRET_TEXT, "sensitive": True}, step_idx=1)

        self.assertNotIn(SECRET_TEXT, json.dumps(event.payload))


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
        self.assertEqual(_event(trace, "action").payload["decision"]["selector"], "#pw")

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

        self.assertEqual(_event(trace, "action").payload["decision"]["text"], "blue shoes")
        self.assertEqual(_event(trace, "model_decision").payload["decision"]["text"], "blue shoes")

    def test_a_step_with_no_decision_emits_no_model_decision_even_if_execution_redacted_text(self) -> None:
        trace = TraceEnvelope.from_agent_result(
            contract_hash="h",
            result=_agent_result(execution={"target": {"selector": "#pw", "text_redacted": True}}),
        )

        self.assertEqual([event.event_type for event in trace.events], ["action"])

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

    def test_text_right_after_a_url_is_scrubbed_as_text_not_swallowed_by_the_url(self) -> None:
        # The URL ends at a quote, so the email after it is text; a bare comma may be inside a URL, so there the email
        # is in the URL's path, which the text scrubber reads too.
        for text in (
            f'{{"url":"https://example.com","email":"{EMAIL}"}}',
            f"see <https://example.com>,{EMAIL}",
            f"https://a.example/x,{EMAIL}",
            f"https://a.example/users/{EMAIL}/profile",
        ):
            with self.subTest(text=text):
                out = self._stored(final_observation={"text": text}).final_observation["text"]

                self.assertNotIn(EMAIL, out)
                self.assertIn("example", out)

    def test_a_url_keeps_its_host_and_clean_path_and_loses_an_email_in_its_query(self) -> None:
        out = self._stored(
            final_observation={"text": f"go to https://a.example/docs/page?email={EMAIL}&x=1, then stop"}
        ).final_observation["text"]

        self.assertNotIn(EMAIL, out)
        self.assertTrue(out.startswith("go to https://a.example/docs/page?"))
        self.assertTrue(out.endswith(", then stop"))

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

    def test_an_apostrophe_inside_a_url_does_not_end_it_but_one_closing_a_quote_does(self) -> None:
        stored = self._stored(extracted_data={"note": f"see https://x.test/s?q=o'neil&email={PLAIN_TOKEN}%40a.com now"})

        self.assertNotIn(PLAIN_TOKEN, json.dumps(stored.extracted_data))
        self.assertIn("see https://x.test/s?q=o", stored.extracted_data["note"])

        url = "https://example.com/a?b=1"
        for text in (
            f"{{'url': '{url}'}}",
            f"['{url}', 'next']",
            f"('{url}': 1)",
            f"'{url}'",
            f"x='{url}';",
            f'{{"url":"{url}","email":"{EMAIL}"}}',
        ):
            with self.subTest(text=text):
                self.assertEqual([url], _URL_PATTERN.findall(text))

    def test_a_url_in_quotes_ends_before_a_closing_quote_followed_by_sentence_punctuation(self) -> None:
        url = "https://example.com/a?b=1"
        for text in (f"see '{url}'.", f"he said '{url}'! ok", f"is it '{url}'?", f"'{url}'. Next"):
            with self.subTest(text=text):
                self.assertEqual([url], _URL_PATTERN.findall(text))
        # An apostrophe inside the URL is still part of it.
        inside = "https://example.com/?q=o'neil&x=1"
        self.assertEqual([inside], _URL_PATTERN.findall(f"look at {inside}"))

    def test_the_file_on_disk_holds_no_url_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = TraceEnvelope(
                contract_hash="h", extracted_data={"link": f"https://x.test/?email={PLAIN_TOKEN}%40a.com"}
            )
            path = envelope.write_json(Path(tmp) / "trace.json")

            self.assertNotIn(PLAIN_TOKEN, path.read_text(encoding="utf-8"))


class ScrubberFollowsSettingsTests(unittest.TestCase):
    def test_the_trace_scrubber_is_rebuilt_when_the_settings_change(self) -> None:
        from unittest.mock import patch

        from app.config import Settings

        def scrubbed_with(settings: Settings) -> str:
            with patch("app.harness.trace.get_settings", return_value=settings):
                return (
                    TraceEnvelope(contract_hash="h", final_observation={"text": EMAIL})
                    .scrubbed()
                    .final_observation["text"]
                )

        self.assertNotIn(EMAIL, scrubbed_with(Settings(PII_SCRUB_ENABLED=True)))
        self.assertEqual(scrubbed_with(Settings(PII_SCRUB_ENABLED=False)), EMAIL)
        self.assertNotIn(EMAIL, scrubbed_with(Settings(PII_SCRUB_ENABLED=True)))


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
