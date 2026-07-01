"""CI-safe tests for the verifier lane adapter (pure dict mapping, no browser)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ADAPTERS = Path(__file__).resolve().parents[2] / "benchmarks" / "adapters"
if str(_ADAPTERS) not in sys.path:
    sys.path.insert(0, str(_ADAPTERS))

from verifier_adapter import (  # noqa: E402
    REQUIRED_FIELDS,
    adapt,
    missing_source_fields,
    to_cuaverifier_input,
    to_mind2web_result,
)

_SAMPLE_RUN = {
    "provider": "openai",
    "model": "gpt-x",
    "goal": "find one recent order status",
    "workflow_profile": "governed",
    "status": "done",
    "steps": [
        {"status": "acted", "decision": {"action": "navigate", "url": "https://shop/orders", "risk_category": "read"}},
        {"status": "acted", "decision": {"action": "click", "element_id": "order-1", "risk_category": "write"}},
    ],
    "final_session": {
        "current_url": "https://shop/orders/1",
        "auth_profile": "shopper",
        "artifact_dir": "/data/artifacts/s1",
        "trace_path": "/data/artifacts/s1/trace.zip",
    },
}


class VerifierAdapterTests(unittest.TestCase):
    def test_mind2web_has_all_required_fields(self) -> None:
        record = to_mind2web_result(_SAMPLE_RUN)
        for field in REQUIRED_FIELDS["online_mind2web"]:
            self.assertIn(field, record)
        self.assertEqual(record["provider"], "openai")
        self.assertEqual(record["workflow_profile"], "governed")
        self.assertEqual(record["auth_profile"], "shopper")
        self.assertEqual(record["final_url"], "https://shop/orders/1")
        self.assertEqual(len(record["action_sequence"]), 2)
        self.assertEqual(record["action_sequence"][0]["action"], "navigate")

    def test_cuaverifier_has_all_required_fields(self) -> None:
        record = to_cuaverifier_input(_SAMPLE_RUN)
        for field in REQUIRED_FIELDS["cuaverifier"]:
            self.assertIn(field, record)
        self.assertEqual(record["task_goal"], "find one recent order status")
        self.assertEqual(record["evidence"]["trace"], "/data/artifacts/s1/trace.zip")

    def test_adapter_never_scores(self) -> None:
        records = adapt(_SAMPLE_RUN)
        for lane in ("online_mind2web", "cuaverifier"):
            self.assertFalse(records[lane]["scored"])
            self.assertIsNone(records[lane]["verifier_result"])

    def test_missing_source_fields_flags_incomplete_run(self) -> None:
        self.assertEqual(missing_source_fields(_SAMPLE_RUN), [])
        gaps = missing_source_fields({"provider": "", "steps": [], "final_session": {}})
        self.assertIn("provider", gaps)
        self.assertIn("steps (action_sequence would be empty)", gaps)


if __name__ == "__main__":
    unittest.main()
