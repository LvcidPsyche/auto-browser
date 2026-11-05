"""TraceEnvelope.from_agent_result carries the screenshots a live agent result holds, not only its actions."""

from __future__ import annotations

import unittest

from app.harness.trace import TraceEnvelope, TraceEvent

SHOT = "/data/artifacts/s1/shot-1.png"


def _result() -> dict:
    return {
        "steps": [
            {
                "status": "ok",
                "decision": {"action": "click"},
                "observation": {
                    "url": "https://x.test/",
                    "screenshot_path": SHOT,
                    "screenshot_url": "/artifacts/s1/shot-1.png",
                },
                "execution": {},
            },
            {
                "status": "ok",
                "decision": {"action": "type"},
                "observation": {"url": "https://x.test/a", "screenshot_path": None},
                "execution": {},
            },
        ],
        "final_session": {"current_url": "https://x.test/done"},
    }


class AgentResultEventTests(unittest.TestCase):
    def test_a_steps_screenshot_comes_before_its_action(self) -> None:
        # The orchestrator observes, then decides and acts, so the screenshot is the page before the action.
        trace = TraceEnvelope.from_agent_result(contract_hash="h", result=_result())

        self.assertEqual(
            [(e.step_idx, e.event_type) for e in trace.events],
            [(1, "screenshot"), (1, "model_decision"), (1, "action"), (2, "model_decision"), (2, "action")],
        )
        self.assertEqual(trace.events[0].payload["screenshot_path"], SHOT)
        self.assertIn("screenshots", trace.evidence)
        self.assertTrue(trace.verify_chain())

    def test_a_result_with_no_screenshots_adds_no_screenshot_events(self) -> None:
        result = _result()
        for step in result["steps"]:
            step["observation"] = {"url": "https://x.test/"}

        trace = TraceEnvelope.from_agent_result(contract_hash="h", result=result)

        self.assertEqual([e.event_type for e in trace.events], ["model_decision", "action", "model_decision", "action"])
        self.assertNotIn("screenshots", trace.evidence)

    def test_a_live_run_that_made_decisions_meets_a_model_decisions_requirement(self) -> None:
        trace = TraceEnvelope.from_agent_result(contract_hash="h", result=_result())

        self.assertIn("model_decisions", trace.evidence)
        decision = next(e for e in trace.events if e.event_type == "model_decision" and e.step_idx == 2)
        self.assertEqual(decision.payload["decision"]["action"], "type")

    def test_a_step_without_a_decision_gets_no_model_decision_event(self) -> None:
        result = {"steps": [{"status": "error", "decision": {}, "observation": {}, "execution": None}]}

        trace = TraceEnvelope.from_agent_result(contract_hash="h", result=result)

        self.assertEqual([e.event_type for e in trace.events], ["action"])
        self.assertNotIn("model_decisions", trace.evidence)

    def test_the_model_decision_event_is_redacted_like_the_action(self) -> None:
        secret = "Sup3rS3cret-Passw0rd!"
        result = {
            "steps": [
                {
                    "status": "ok",
                    "decision": {"action": "type", "selector": "#pw", "text": secret, "sensitive": True},
                    "observation": {"url": "https://x.test/"},
                    "execution": {},
                }
            ]
        }

        trace = TraceEnvelope.from_agent_result(contract_hash="h", result=result)

        decision = next(e for e in trace.events if e.event_type == "model_decision")
        self.assertEqual(decision.payload["decision"]["text"], "[REDACTED]")
        self.assertNotIn(secret, trace.model_dump_json())


class ScreenshotEvidenceTests(unittest.TestCase):
    def _kinds(self, payload: dict) -> set[str]:
        recorder_event = TraceEvent(run_id="r", step_idx=1, event_type="screenshot", payload=payload).with_hash()
        return TraceEnvelope(contract_hash="h", events=[recorder_event]).evidence_kinds()

    def test_the_mock_attempts_unavailable_screenshot_is_not_screenshot_evidence(self) -> None:
        self.assertNotIn("screenshots", self._kinds({"available": False, "reason": "mock_mode"}))

    def test_an_unavailable_screenshot_with_a_path_is_not_evidence_either(self) -> None:
        self.assertNotIn("screenshots", self._kinds({"available": False, "screenshot_path": SHOT}))

    def test_a_screenshot_with_a_path_or_a_url_is_evidence(self) -> None:
        self.assertIn("screenshots", self._kinds({"screenshot_path": SHOT}))
        self.assertIn("screenshots", self._kinds({"screenshot_url": "/artifacts/s1/shot-1.png"}))
