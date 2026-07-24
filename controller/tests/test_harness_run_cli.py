"""Tests for the harness CLI entry point (`python -m app.harness.run`).

The mock-observation flags are what make a convergence run deterministic
offline, so their parsing is the difference between a reproducible local run
and one that silently drops the operator's input.
"""
from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.harness import run as harness_run

CONTRACT = {
    "id": "task-cli-001",
    "goal": "Open a product page",
    "postconditions": [{"kind": "url_contains", "value": "example.com"}],
}


def _args(**overrides: object) -> argparse.Namespace:
    base = {"mock_final_observation": "", "mock_final_url": "", "mock_final_text": ""}
    base.update(overrides)
    return argparse.Namespace(**base)


class LoadMockObservationTests(unittest.TestCase):
    def test_returns_none_when_nothing_supplied(self) -> None:
        self.assertIsNone(harness_run._load_mock_observation(_args()))

    def test_parses_inline_json_object(self) -> None:
        loaded = harness_run._load_mock_observation(_args(mock_final_observation='{"url": "https://example.com/a"}'))
        self.assertEqual(loaded, {"url": "https://example.com/a"})

    def test_parses_json_from_a_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "observation.json"
            source.write_text(json.dumps({"url": "https://example.com/b", "text": "ready"}), encoding="utf-8")

            loaded = harness_run._load_mock_observation(_args(mock_final_observation=str(source)))

        self.assertEqual(loaded, {"url": "https://example.com/b", "text": "ready"})

    def test_nonexistent_path_is_parsed_as_literal_json(self) -> None:
        # A missing path falls back to treating the argument as JSON text, so a
        # typo'd path surfaces as a JSON decode error rather than silently
        # running with no mock observation at all.
        with self.assertRaises(json.JSONDecodeError):
            harness_run._load_mock_observation(_args(mock_final_observation="/no/such/file.json"))

    def test_rejects_json_that_is_not_an_object(self) -> None:
        for raw in ("[1, 2]", '"a string"', "5"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                harness_run._load_mock_observation(_args(mock_final_observation=raw))

    def test_convenience_flags_populate_url_and_text(self) -> None:
        loaded = harness_run._load_mock_observation(
            _args(mock_final_url="https://example.com/c", mock_final_text="done")
        )
        self.assertEqual(loaded, {"url": "https://example.com/c", "text": "done"})

    def test_convenience_flags_override_the_observation_payload(self) -> None:
        loaded = harness_run._load_mock_observation(
            _args(
                mock_final_observation='{"url": "https://old.example/x", "keep": true}',
                mock_final_url="https://example.com/new",
            )
        )
        self.assertEqual(loaded, {"url": "https://example.com/new", "keep": True})


class HarnessRunMainTests(unittest.TestCase):
    """End-to-end CLI runs — deterministic because the observation is mocked."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.contract_path = self.root / "contract.json"
        self.contract_path.write_text(json.dumps(CONTRACT), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *extra: str) -> tuple[int, str]:
        argv = [
            "run.py",
            "--contract",
            str(self.contract_path),
            "--root",
            str(self.root / "harness"),
            *extra,
        ]
        buffer = io.StringIO()
        with patch.object(harness_run.sys, "argv", argv) if hasattr(harness_run, "sys") else patch("sys.argv", argv):
            with redirect_stdout(buffer):
                exit_code = harness_run.main()
        return exit_code, buffer.getvalue()

    def test_satisfied_postcondition_converges_and_exits_zero(self) -> None:
        exit_code, output = self._run("--mock-final-url", "https://example.com/product/1")

        self.assertEqual(exit_code, 0)
        self.assertIn("converged", output)

    def test_unsatisfied_postcondition_reports_status_instead_of_crashing(self) -> None:
        # Regression: `candidate` is present-but-null on a non-converged run, so
        # payload.get("candidate", {}) returned None and the summary line raised
        # AttributeError -- crashing the CLI exactly when an operator is trying
        # to read why the run failed.
        exit_code, output = self._run("--mock-final-url", "https://not-the-target.test/product/1")

        self.assertEqual(exit_code, 1)
        self.assertIn("unconverged", output)
        self.assertRegex(output, r"harness run \w+: unconverged after \d+ attempt\(s\)")
        self.assertNotIn("staged candidate", output)

    def test_json_flag_emits_the_full_record(self) -> None:
        exit_code, output = self._run("--mock-final-url", "https://example.com/product/1", "--json")

        self.assertEqual(exit_code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["status"], "converged")
        self.assertTrue(payload["id"])


if __name__ == "__main__":
    unittest.main()
