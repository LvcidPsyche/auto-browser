"""CI-safe validation of the WebArena Stage 0 contracts (no browser, no env)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_WEBARENA = Path(__file__).resolve().parents[2] / "benchmarks" / "webarena"
if str(_WEBARENA) not in sys.path:
    sys.path.insert(0, str(_WEBARENA))

from stage0_contracts import (  # noqa: E402
    EXPECTED_TASK_COUNT,
    REQUIRED_EVIDENCE,
    is_scorable,
    load_contracts,
    validate_manifest,
)


class WebArenaStage0ContractTests(unittest.TestCase):
    def test_manifest_is_well_formed(self) -> None:
        self.assertEqual(validate_manifest(), [])

    def test_five_contracts_load(self) -> None:
        contracts = load_contracts()
        self.assertEqual(len(contracts), EXPECTED_TASK_COUNT)
        ids = {c.id for c in contracts}
        self.assertIn("shopping-order-status-read", ids)
        self.assertIn("cms-draft-review-governed", ids)

    def test_every_contract_requires_all_evidence(self) -> None:
        for contract in load_contracts():
            for evidence in REQUIRED_EVIDENCE:
                self.assertIn(evidence, contract.required_evidence, f"{contract.id} missing {evidence}")

    def test_evidence_plan_paths(self) -> None:
        contract = load_contracts()[0]
        with tempfile.TemporaryDirectory() as tmp:
            plan = contract.evidence_plan(tmp)
            self.assertTrue(str(plan["trace"]).endswith("trace.zip"))
            self.assertTrue(str(plan["actions"]).endswith("actions.json"))
            self.assertTrue(str(plan["model_decisions"]).endswith("model_decisions.json"))
            self.assertEqual(plan["dir"].name, contract.id)

    def test_lane_is_tracked_only_until_pinned(self) -> None:
        # environment.revision is null and competitive_score_allowed is false.
        self.assertFalse(is_scorable())


if __name__ == "__main__":
    unittest.main()
