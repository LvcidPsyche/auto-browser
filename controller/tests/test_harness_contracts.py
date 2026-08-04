from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace

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
