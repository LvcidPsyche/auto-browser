"""A staged skill stores a scrubbed trace, so the drift monitor must expect scrubbed text too."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

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


class DriftOnUrlContainsTests(unittest.IsolatedAsyncioTestCase):
    async def _drift(self, expected: str, final_url: str) -> str:
        contract = TaskContract(
            id="drift-url",
            goal="Open the page.",
            postconditions=[Postcondition(kind="url_contains", value=expected)],
            budget=Budget(max_attempts=1, max_steps=2),
        )
        observation = {"url": final_url, "text": "done", "accessibility_outline": {}}
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)
            record = await service.start_convergence(contract, mock_final_observation=observation)
            self.assertEqual(record.status, "converged")
            drift = await service.drift_monitor.check_candidate(record.candidate.skill_id)
            return drift.status

    async def test_an_email_in_the_url_path_stays_healthy(self) -> None:
        self.assertEqual(await self._drift(f"/u/{EMAIL}/settings", f"https://app.test/u/{EMAIL}/settings"), "healthy")

    async def test_an_email_in_the_url_query_stays_healthy(self) -> None:
        self.assertEqual(await self._drift(f"?email={EMAIL}", f"https://app.test/done?email={EMAIL}"), "healthy")

    async def test_a_query_piece_without_its_question_mark_stays_healthy(self) -> None:
        # `email=...` has no `?`, but it is a piece of the query, and the stored query was scrubbed.
        for expected in (f"email={EMAIL}", f"&email={EMAIL}"):
            with self.subTest(expected=expected):
                final_url = f"https://app.test/done?next=1&email={EMAIL}"
                self.assertEqual(await self._drift(expected, final_url), "healthy")

    async def test_a_bare_email_that_stood_in_the_query_stays_healthy(self) -> None:
        # The bare value is scrubbed to the same replacement the stored query holds, so it is checked as "some
        # redaction is present".
        self.assertEqual(await self._drift(EMAIL, f"https://app.test/done?email={EMAIL}"), "healthy")


class DriftScrubberFailureTests(unittest.IsolatedAsyncioTestCase):
    def test_a_scrubber_that_returns_nothing_leaves_the_expectation_as_written(self) -> None:
        from app.harness.drift import _scrub_url_contains

        with patch("app.harness.drift._scrub_url", return_value=""):
            self.assertEqual(_scrub_url_contains("email=alice"), "email=alice")

    async def test_a_scrubber_that_raises_makes_the_check_unverified_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)
            contract = TaskContract(
                id="drift-boom",
                goal="Open the page.",
                postconditions=[Postcondition(kind="url_contains", value="email=alice")],
                budget=Budget(max_attempts=1, max_steps=2),
            )
            record = await service.start_convergence(
                contract, mock_final_observation={"url": "https://app.test/?email=alice", "text": "done"}
            )

            with patch("app.harness.drift._scrub_url", side_effect=RuntimeError("scrubber bug")):
                drift = await service.drift_monitor.check_candidate(record.candidate.skill_id)

            self.assertEqual(drift.status, "unverified")
            self.assertIn("RuntimeError", " ".join(drift.notes))

    async def test_a_url_scrubber_that_fails_on_a_query_or_fragment_value_never_matches_every_page(self) -> None:
        # The PII scrubber's url() failing is caught inside scrub_url, which hands back the value's path: "" for a
        # value that starts with `?` or `#`. An expectation of "" is in every URL, so the check must not say healthy.
        from app.pii_scrub import PiiScrubber

        for expected, final_url in (
            (f"?email={EMAIL}", f"https://app.test/done?email={EMAIL}"),
            (f"#email={EMAIL}", f"https://app.test/done#email={EMAIL}"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                service = HarnessService(tmp)
                contract = TaskContract(
                    id="drift-url-scrubber",
                    goal="Open the page.",
                    postconditions=[Postcondition(kind="url_contains", value=expected)],
                    budget=Budget(max_attempts=1, max_steps=2),
                )
                record = await service.start_convergence(
                    contract, mock_final_observation={"url": final_url, "text": "done"}
                )
                self.assertEqual(record.status, "converged")

                with patch.object(PiiScrubber, "url", side_effect=RuntimeError("scrubber bug")):
                    drift = await service.drift_monitor.check_candidate(record.candidate.skill_id)

                self.assertNotEqual(drift.status, "healthy", drift.notes)

    async def test_a_url_inside_a_text_or_dom_expectation_is_kept_as_written_when_its_scrub_fails(self) -> None:
        # text_contains and dom_contains scrub each URL in the value as a URL. A failing URL scrub hands back the
        # URL's path, so "Done: https://app.test/done?email=..." became "Done: https://app.test/done", which the
        # stored text contains whatever its query said.
        from app.pii_scrub import PiiScrubber

        value = f"Done: https://app.test/done?email={EMAIL}"
        for kind, observation in (
            ("text_contains", {"url": "https://app.test/done", "text": value}),
            ("dom_contains", {"url": "https://app.test/done", "text": "done", "dom": value}),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                service = HarnessService(tmp)
                contract = TaskContract(
                    id="drift-url-span",
                    goal="Open the page.",
                    postconditions=[Postcondition(kind=kind, value=value)],
                    budget=Budget(max_attempts=1, max_steps=2),
                )
                record = await service.start_convergence(contract, mock_final_observation=observation)
                self.assertEqual(record.status, "converged")

                with patch.object(PiiScrubber, "url", side_effect=RuntimeError("scrubber bug")):
                    drift = await service.drift_monitor.check_candidate(record.candidate.skill_id)

                self.assertNotEqual(drift.status, "healthy", drift.notes)

    async def test_a_url_inside_a_text_expectation_still_matches_when_its_scrub_works(self) -> None:
        value = f"Done: https://app.test/done?email={EMAIL}"
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)
            contract = TaskContract(
                id="drift-url-span-ok",
                goal="Open the page.",
                postconditions=[Postcondition(kind="text_contains", value=value)],
                budget=Budget(max_attempts=1, max_steps=2),
            )
            record = await service.start_convergence(
                contract, mock_final_observation={"url": "https://app.test/done", "text": value}
            )

            drift = await service.drift_monitor.check_candidate(record.candidate.skill_id)

            self.assertEqual(drift.status, "healthy", drift.notes)

    async def test_a_url_scrubber_that_fails_on_a_path_with_a_query_or_fragment_never_matches_every_page(self) -> None:
        # The same failure on `/?...` or `/#...` hands back "/", which every URL also contains: a scrub that lost the
        # value's query or fragment is a failed one, and the value is kept as written.
        from app.pii_scrub import PiiScrubber

        for expected, final_url in (
            (f"/?email={EMAIL}", f"https://app.test/?email={EMAIL}"),
            (f"/#email={EMAIL}", f"https://app.test/#email={EMAIL}"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                service = HarnessService(tmp)
                contract = TaskContract(
                    id="drift-url-scrubber-path",
                    goal="Open the page.",
                    postconditions=[Postcondition(kind="url_contains", value=expected)],
                    budget=Budget(max_attempts=1, max_steps=2),
                )
                record = await service.start_convergence(
                    contract, mock_final_observation={"url": final_url, "text": "done"}
                )
                self.assertEqual(record.status, "converged")

                with patch.object(PiiScrubber, "url", side_effect=RuntimeError("scrubber bug")):
                    drift = await service.drift_monitor.check_candidate(record.candidate.skill_id)

                self.assertNotEqual(drift.status, "healthy", drift.notes)


if __name__ == "__main__":
    unittest.main()
