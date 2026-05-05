from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.agent_eval import (
    build_matrix,
    load_cases,
    plan_payload,
    render_markdown_report,
    score_from_result_dir,
    score_result,
    summarize_scores,
)


class AgentEvalTests(unittest.TestCase):
    def test_load_matrix_and_score_success(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            cases_path = Path(tempdir) / "cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "case-1",
                                "goal": "finish",
                                "providers": ["openai"],
                                "workflow_profiles": ["fast", "governed"],
                                "expected_statuses": ["done"],
                                "expect_url_contains": ["example.com"],
                                "expect_actions": ["done"],
                                "min_step_count": 1,
                                "max_step_count": 2,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            cases = load_cases(cases_path)
            matrix = build_matrix(cases)
            result = {
                "status": "done",
                "steps": [
                    {
                        "decision": {"action": "done"},
                        "observation": {"url": "https://example.com"},
                    }
                ],
                "final_session": {"current_url": "https://example.com"},
            }
            score = score_result(matrix[0], result)
            summary = summarize_scores([score])

            self.assertEqual([spec.workflow_profile for spec in matrix], ["fast", "governed"])
            self.assertTrue(score.success)
            self.assertEqual(score.score, 1.0)
            self.assertEqual(summary["openai/fast"]["successes"], 1)
            self.assertEqual(plan_payload(matrix)["runs"][0]["result_file"], "case-1__openai__fast.json")
            self.assertIn("| case-1 | openai | fast | PASS | 1.0000 | - |", render_markdown_report(matrix, scores=[score]))
            self.assertIn("Planned runs: 2", render_markdown_report(matrix))

    def test_score_from_result_dir_marks_missing_results_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            cases_path = Path(tempdir) / "cases.json"
            cases_path.write_text(
                json.dumps({"cases": [{"id": "missing-case", "goal": "finish", "providers": ["openai"]}]}),
                encoding="utf-8",
            )

            matrix = build_matrix(load_cases(cases_path), workflow_profiles=("fast",))
            scores = score_from_result_dir(matrix, tempdir)

            self.assertFalse(scores[0].success)
            self.assertIn("missing result file", scores[0].criteria[-1].detail)


if __name__ == "__main__":
    unittest.main()
