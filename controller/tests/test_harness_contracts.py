from __future__ import annotations

import asyncio
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from pydantic import ValidationError

from app.harness import EvidenceRequirement, ForbiddenState, Postcondition, TaskContract
from app.harness.verifier.base import EnsembleVerifier, VerificationResult
from app.harness.verifier.programmatic import ProgrammaticVerifier
from app.harness.verifier.uv import UniversalVerifierAdapter


class ContractModelTests(unittest.TestCase):
    def _base_contract(self) -> TaskContract:
        return TaskContract(
            id="task-001",
            goal="Open a product page",
            postconditions=[
                Postcondition(kind="url_contains", value="example.com"),
            ],
        )

    def test_task_contract_requires_postconditions(self) -> None:
        with self.assertRaises(ValidationError):
            TaskContract(id="task-002", goal="No postconditions", postconditions=[])

    def test_task_contract_rejects_invalid_url_matcher(self) -> None:
        with self.assertRaises(ValidationError):
            Postcondition(kind="url_matches", value="(unclosed")

    def test_forbidden_state_requires_value(self) -> None:
        with self.assertRaises(ValidationError):
            ForbiddenState(kind="url_contains", value="")

    def test_sentinel_forbidden_states_do_not_require_values(self) -> None:
        self.assertEqual(ForbiddenState(kind="captcha_screen").kind, "captcha_screen")
        self.assertEqual(ForbiddenState(kind="payment_screen").kind, "payment_screen")
        self.assertEqual(ForbiddenState(kind="login_redirect").kind, "login_redirect")

    def test_task_contract_reports_required_evidence_kinds(self) -> None:
        contract = TaskContract(
            id="task-003",
            goal="Collect evidence",
            postconditions=[Postcondition(kind="text_contains", value="ready")],
            evidence_required=[
                EvidenceRequirement(kind="trace"),
                EvidenceRequirement(kind="actions"),
                EvidenceRequirement(kind="screenshots", required=False),
            ],
        )

        self.assertEqual(contract.required_evidence_kinds, {"trace", "actions"})


class ProgrammaticVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_programmatic_url_contains_and_text_conditions(self) -> None:
        contract = TaskContract(
            id="task-verify-1",
            goal="verify url and text",
            postconditions=[
                Postcondition(kind="url_contains", value="example.com/checkout"),
                Postcondition(kind="text_contains", value="Order confirmed"),
            ],
            evidence_required=[EvidenceRequirement(kind="trace")],
        )

        trace = {
            "final_observation": {
                "url": "https://example.com/checkout/success",
                "text": "Order confirmed for invoice #12.",
            },
            "evidence": ["trace"],
        }
        result = await ProgrammaticVerifier().verify(contract, trace)

        self.assertTrue(result.passed)
        self.assertEqual(
            result.satisfied_postconditions,
            [
                "postcondition[0]:url_contains:example.com/checkout",
                "postcondition[1]:text_contains:Order confirmed",
            ],
        )
        self.assertEqual(result.missing_evidence, [])

    async def test_programmatic_dom_condition_and_url_matches(self) -> None:
        contract = TaskContract(
            id="task-verify-2",
            goal="Check dom and url pattern",
            postconditions=[
                Postcondition(kind="dom_contains", value="pricing-card"),
                Postcondition(kind="url_matches", value=r"checkout\?id=\d+"),
            ],
            evidence_required=[EvidenceRequirement(kind="trace"), EvidenceRequirement(kind="actions")],
        )

        trace = {
            "final_observation": {
                "url": "https://shop.example.com/checkout?id=42",
                "dom": "<div>Pricing-card</div><script>ok</script>",
            },
            "evidence_kinds": ["trace", "actions"],
        }
        result = await ProgrammaticVerifier().verify(contract, trace)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.satisfied_postconditions), 2)

    async def test_programmatic_forbidden_text_and_url_checks(self) -> None:
        contract = TaskContract(
            id="task-verify-3",
            goal="Reject forbidden conditions",
            postconditions=[Postcondition(kind="url_contains", value="example.com")],
            forbidden_states=[
                {"kind": "url_contains", "value": "logout"},
                {"kind": "text_contains", "value": "password"},
            ],
            evidence_required=[EvidenceRequirement(kind="trace")],
        )
        trace = {
            "final_observation": {
                "url": "https://example.com/logout?next=/dashboard",
                "text": "Enter your password to continue.",
            },
            "evidence": ["trace"],
        }

        result = await ProgrammaticVerifier().verify(contract, trace)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.forbidden_state_hits), 2)
        self.assertIn("forbidden[0]:url_contains:logout", result.forbidden_state_hits)
        self.assertIn("forbidden[1]:text_contains:password", result.forbidden_state_hits)

    async def test_programmatic_forbidden_sentinel_checks(self) -> None:
        contract = TaskContract(
            id="task-verify-sentinel",
            goal="Reject sentinel states",
            postconditions=[Postcondition(kind="url_contains", value="example.com")],
            forbidden_states=[
                ForbiddenState(kind="captcha_screen"),
                ForbiddenState(kind="payment_screen"),
                ForbiddenState(kind="login_redirect"),
            ],
            evidence_required=[EvidenceRequirement(kind="trace")],
        )
        trace = {
            "final_observation": {
                "url": "https://example.com/login",
                "text": "Please sign in. Payment requires credit card after captcha.",
            },
            "evidence": ["trace"],
        }

        result = await ProgrammaticVerifier().verify(contract, trace)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.forbidden_state_hits), 3)

    async def test_programmatic_missing_required_evidence(self) -> None:
        contract = TaskContract(
            id="task-verify-4",
            goal="Require screenshots",
            postconditions=[Postcondition(kind="url_contains", value="example.com")],
            evidence_required=[
                EvidenceRequirement(kind="trace"),
                EvidenceRequirement(kind="screenshots"),
            ],
        )
        trace = {
            "final_observation": {"url": "https://example.com/page"},
            "evidence": ["trace"],
        }

        result = await ProgrammaticVerifier().verify(contract, trace)

        self.assertFalse(result.passed)
        self.assertEqual(result.missing_evidence, ["screenshots"])

    async def test_programmatic_supports_object_traces(self) -> None:
        contract = TaskContract(
            id="task-verify-5",
            goal="Supports final_observation on object trace",
            postconditions=[Postcondition(kind="text_contains", value="welcome")],
            evidence_required=[EvidenceRequirement(kind="actions")],
        )
        trace = SimpleNamespace(
            final_observation={"text": "welcome home"},
            evidence_kinds=lambda: ["actions"],
        )

        result = await ProgrammaticVerifier().verify(contract, trace)

        self.assertTrue(result.passed)
        self.assertEqual(result.satisfied_postconditions, ["postcondition[0]:text_contains:welcome"])


class UniversalVerifierAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_uv_adapter_reports_unavailable_without_command(self) -> None:
        contract = TaskContract(
            id="task-uv-1",
            goal="uv unavailable",
            postconditions=[{"kind": "url_contains", "value": "example.com"}],
            evidence_required=[EvidenceRequirement(kind="trace")],
        )
        result = await UniversalVerifierAdapter().verify(
            contract, {"final_observation": {"url": "https://example.com"}}
        )

        self.assertIsNone(result.passed)
        self.assertEqual(result.backend, "uv")
        self.assertIn("not configured", result.notes.lower())

    async def test_uv_adapter_reports_unavailable_when_binary_missing(self) -> None:
        contract = TaskContract(
            id="task-uv-2",
            goal="uv missing command",
            postconditions=[{"kind": "url_contains", "value": "example.com"}],
            evidence_required=[EvidenceRequirement(kind="trace")],
        )
        adapter = UniversalVerifierAdapter(command=["this-command-does-not-exist"])
        self.assertFalse(adapter.available)

        result = await adapter.verify(contract, {"final_observation": {"url": "https://example.com"}})

        self.assertIsNone(result.passed)
        self.assertIn("was not found", result.notes)

    async def test_uv_adapter_reports_unavailable_for_invalid_output_schema(self) -> None:
        contract = TaskContract(
            id="task-uv-schema",
            goal="uv invalid schema",
            postconditions=[{"kind": "url_contains", "value": "example.com"}],
        )
        adapter = UniversalVerifierAdapter(
            command=[
                sys.executable,
                "-c",
                "import json; print(json.dumps({'passed': True, 'confidence': 2.0}))",
            ]
        )

        result = await adapter.verify(contract, {"final_observation": {"url": "https://example.com"}})

        self.assertIsNone(result.passed)
        self.assertIn("schema", result.notes.lower())

    async def _run_and_capture_child(self, adapter: UniversalVerifierAdapter, started: list) -> None:
        contract = TaskContract(
            id="task-uv-kill",
            goal="uv child must not outlive the call",
            postconditions=[{"kind": "url_contains", "value": "example.com"}],
        )
        original = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            process = await original(*args, **kwargs)
            started.append(process)
            return process

        with mock.patch("app.harness.verifier.uv.asyncio.create_subprocess_exec", spy):
            await adapter.verify(contract, {})

    async def test_uv_child_is_killed_and_reaped_when_the_caller_cancels(self) -> None:
        """A cancelled verify() (the harness wall-clock wait_for, shutdown) skipped
        process.kill(): only the adapter's own timeout path killed the child."""
        started: list = []
        adapter = UniversalVerifierAdapter(
            command=[sys.executable, "-c", "import time; time.sleep(120)"], timeout_seconds=300
        )
        task = asyncio.create_task(self._run_and_capture_child(adapter, started))
        try:
            deadline = asyncio.get_running_loop().time() + 30
            while not started:
                self.assertLess(asyncio.get_running_loop().time(), deadline, "child never started")
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            # returncode is only set once the child has exited and been reaped.
            self.assertIsNotNone(started[0].returncode)
        finally:
            for process in started:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    async def test_uv_child_is_killed_on_its_own_timeout(self) -> None:
        started: list = []
        adapter = UniversalVerifierAdapter(
            command=[sys.executable, "-c", "import time; time.sleep(120)"], timeout_seconds=1
        )
        try:
            await self._run_and_capture_child(adapter, started)
            self.assertIsNotNone(started[0].returncode)
        finally:
            for process in started:
                if process.returncode is None:
                    process.kill()
                    await process.wait()


class ProgrammaticRegexTests(unittest.IsolatedAsyncioTestCase):
    """A caller-supplied url_matches pattern ran in-process: catastrophic
    backtracking stalled the event loop and wait_for could not interrupt it."""

    # (a+)+$ takes ~11 s in-process on 26 characters and quadruples per two more.
    CATASTROPHIC = "(a+)+$"
    CATASTROPHIC_URL = "a" * 26 + "!"

    def _contract(self, *, postcondition: str | None = None, forbidden: str | None = None) -> TaskContract:
        return TaskContract(
            id="task-regex",
            goal="regex handling",
            postconditions=[
                Postcondition(kind="url_matches", value=postcondition)
                if postcondition is not None
                else Postcondition(kind="url_contains", value="example")
            ],
            forbidden_states=[ForbiddenState(kind="url_matches", value=forbidden)] if forbidden is not None else [],
        )

    async def test_url_matches_postcondition_and_forbidden_state_still_work(self) -> None:
        trace = {
            "final_observation": {"url": "https://shop.example.com/checkout?id=42"},
            "evidence_kinds": ["trace", "actions"],
        }
        verifier = ProgrammaticVerifier()

        hit = await verifier.verify(self._contract(postcondition=r"checkout\?id=\d+"), trace)
        miss = await verifier.verify(self._contract(postcondition=r"cart\?id=\d+"), trace)
        forbidden_hit = await verifier.verify(self._contract(forbidden=r"checkout\?id=\d+"), trace)
        forbidden_clear = await verifier.verify(self._contract(forbidden=r"admin"), trace)

        self.assertTrue(hit.passed)
        self.assertFalse(miss.passed)
        self.assertEqual(miss.failed_postconditions, ["postcondition[0]:url_matches:cart\\?id=\\d+"])
        self.assertFalse(forbidden_hit.passed)
        self.assertTrue(forbidden_clear.passed)

    async def test_catastrophic_pattern_times_out_without_stalling_the_event_loop(self) -> None:
        gaps: list[float] = []
        stop = asyncio.Event()

        async def ticker() -> None:
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        verifier = ProgrammaticVerifier(regex_timeout_seconds=0.5)
        trace = {"final_observation": {"url": self.CATASTROPHIC_URL}, "evidence_kinds": ["trace", "actions"]}
        result = await verifier.verify(self._contract(postcondition=self.CATASTROPHIC), trace)
        stop.set()
        await task

        # In-process the stall is ~11 s; a free loop ticks every ~10 ms. 1.5 s
        # leaves a wide margin for process start-up on a loaded CI box.
        self.assertLess(max(gaps), 1.5)
        self.assertFalse(result.passed)
        self.assertEqual(result.failed_postconditions, [f"postcondition[0]:url_matches:{self.CATASTROPHIC}"])
        self.assertEqual(result.details["regex_timeouts"], [f"postcondition[0]:url_matches:{self.CATASTROPHIC}"])
        self.assertIn("did not finish", result.notes)

    async def test_forbidden_pattern_that_cannot_be_ruled_out_counts_as_hit(self) -> None:
        verifier = ProgrammaticVerifier(regex_timeout_seconds=0.5)
        trace = {"final_observation": {"url": self.CATASTROPHIC_URL}, "evidence_kinds": ["trace", "actions"]}

        result = await verifier.verify(self._contract(forbidden=self.CATASTROPHIC), trace)

        self.assertFalse(result.passed)
        self.assertEqual(result.forbidden_state_hits, [f"forbidden[0]:url_matches:{self.CATASTROPHIC}"])
        self.assertEqual(result.details["regex_timeouts"], result.forbidden_state_hits)

    def _many(self, postconditions: list[str], forbidden: list[str] | None = None) -> TaskContract:
        return TaskContract(
            id="task-regex-many",
            goal="regex handling",
            postconditions=[Postcondition(kind="url_matches", value=value) for value in postconditions],
            forbidden_states=[ForbiddenState(kind="url_matches", value=value) for value in forbidden or []],
        )

    @staticmethod
    def _count_spawns() -> tuple[list, object]:
        started: list = []
        original = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            process = await original(*args, **kwargs)
            started.append(process)
            return process

        return started, mock.patch("app.harness.verifier.programmatic.asyncio.create_subprocess_exec", spy)

    async def test_every_url_matches_pattern_is_tested_in_one_child(self) -> None:
        trace = {
            "final_observation": {"url": "https://shop.example.com/checkout?id=42"},
            "evidence_kinds": ["trace", "actions"],
        }
        started, patched = self._count_spawns()

        with patched:
            result = await ProgrammaticVerifier().verify(
                self._many([r"checkout\?id=\d+", r"shop\.example"], forbidden=[r"admin"]), trace
            )

        self.assertTrue(result.passed)
        self.assertEqual(len(started), 1)

    async def test_results_keep_their_condition_when_patterns_share_a_child(self) -> None:
        trace = {"final_observation": {"url": "https://shop.example.com/checkout?id=42"}, "evidence_kinds": []}
        contract = TaskContract(
            id="task-order",
            goal="order",
            postconditions=[
                Postcondition(kind="url_matches", value=r"checkout"),
                Postcondition(kind="url_contains", value="example"),
                Postcondition(kind="url_matches", value=r"cart"),
                Postcondition(kind="url_matches", value=r"id=\d+"),
            ],
            forbidden_states=[
                ForbiddenState(kind="url_matches", value=r"admin"),
                ForbiddenState(kind="url_matches", value=r"shop"),
            ],
        )

        result = await ProgrammaticVerifier().verify(contract, trace)

        self.assertEqual(result.failed_postconditions, ["postcondition[2]:url_matches:cart"])
        self.assertEqual(
            result.satisfied_postconditions,
            [
                "postcondition[0]:url_matches:checkout",
                "postcondition[1]:url_contains:example",
                "postcondition[3]:url_matches:id=\\d+",
            ],
        )
        self.assertEqual(result.forbidden_state_hits, ["forbidden[1]:url_matches:shop"])
        self.assertEqual(result.details["regex_timeouts"], [])

    async def test_a_stuck_pattern_times_out_alone_and_the_rest_still_run(self) -> None:
        trace = {"final_observation": {"url": self.CATASTROPHIC_URL}, "evidence_kinds": []}
        started, patched = self._count_spawns()
        verifier = ProgrammaticVerifier(regex_timeout_seconds=0.5)

        with patched:
            result = await verifier.verify(
                self._many([r"^a+!$", self.CATASTROPHIC, r"a!"], forbidden=[r"nomatch"]), trace
            )

        key = f"postcondition[1]:url_matches:{self.CATASTROPHIC}"
        self.assertEqual(result.details["regex_timeouts"], [key])
        self.assertEqual(result.failed_postconditions, [key])
        # The pattern before the stuck one and the ones after it keep their own verdicts.
        self.assertEqual(
            result.satisfied_postconditions,
            ["postcondition[0]:url_matches:^a+!$", "postcondition[2]:url_matches:a!"],
        )
        self.assertEqual(result.forbidden_state_hits, [])
        # One child until the stuck pattern, one for everything after it.
        self.assertEqual(len(started), 2)
        for process in started:
            self.assertIsNotNone(process.returncode)

    async def test_each_stuck_pattern_gets_its_own_timeout(self) -> None:
        trace = {"final_observation": {"url": self.CATASTROPHIC_URL}, "evidence_kinds": []}
        verifier = ProgrammaticVerifier(regex_timeout_seconds=0.5)

        result = await verifier.verify(self._many([self.CATASTROPHIC], forbidden=[r"(?:a+)+$", r"a!"]), trace)

        self.assertEqual(
            result.details["regex_timeouts"],
            [
                f"postcondition[0]:url_matches:{self.CATASTROPHIC}",
                "forbidden[0]:url_matches:(?:a+)+$",
            ],
        )
        self.assertEqual(
            result.forbidden_state_hits, ["forbidden[0]:url_matches:(?:a+)+$", "forbidden[1]:url_matches:a!"]
        )

    async def test_regex_child_is_killed_when_the_caller_cancels(self) -> None:
        started: list = []
        original = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            process = await original(*args, **kwargs)
            started.append(process)
            return process

        verifier = ProgrammaticVerifier(regex_timeout_seconds=300)
        trace = {"final_observation": {"url": self.CATASTROPHIC_URL}, "evidence_kinds": ["trace", "actions"]}
        try:
            with mock.patch("app.harness.verifier.programmatic.asyncio.create_subprocess_exec", spy):
                task = asyncio.create_task(verifier.verify(self._contract(postcondition=self.CATASTROPHIC), trace))
                deadline = asyncio.get_running_loop().time() + 30
                while not started:
                    self.assertLess(asyncio.get_running_loop().time(), deadline, "child never started")
                    await asyncio.sleep(0.01)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            # returncode is only set once the child has exited and been reaped.
            self.assertIsNotNone(started[0].returncode)
        finally:
            for process in started:
                if process.returncode is None:
                    process.kill()
                    await process.wait()


class _StaticVerifier:
    def __init__(self, result: VerificationResult):
        self.result = result

    async def verify(self, contract, trace):
        return self.result


class EnsembleVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensemble_abstains_on_disagreement(self) -> None:
        contract = TaskContract(
            id="task-ensemble",
            goal="ensemble disagreement",
            postconditions=[{"kind": "url_contains", "value": "example.com"}],
        )
        verifier = EnsembleVerifier(
            [
                _StaticVerifier(VerificationResult(passed=True, confidence=0.9, backend="a")),
                _StaticVerifier(VerificationResult(passed=False, confidence=0.9, backend="b")),
            ]
        )

        result = await verifier.verify(contract, {"final_observation": {"url": "https://example.com"}})

        self.assertIsNone(result.passed)
        self.assertIn("disagreement", result.notes.lower())

    async def test_ensemble_abstains_with_only_one_decisive_backend(self) -> None:
        contract = TaskContract(
            id="task-ensemble-partial",
            goal="ensemble partial availability",
            postconditions=[{"kind": "url_contains", "value": "example.com"}],
        )
        verifier = EnsembleVerifier(
            [
                _StaticVerifier(VerificationResult(passed=True, confidence=1.0, backend="programmatic")),
                _StaticVerifier(VerificationResult(passed=None, confidence=0.0, backend="uv")),
            ]
        )

        result = await verifier.verify(contract, {"final_observation": {"url": "https://example.com"}})

        self.assertIsNone(result.passed)
        self.assertIn("insufficient", result.notes.lower())
