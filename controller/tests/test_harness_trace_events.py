"""TraceEnvelope.from_agent_result carries the screenshots a live agent result holds, not only its actions."""

from __future__ import annotations

import unittest

from app.harness.trace import TraceEnvelope

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
    def test_a_step_with_a_screenshot_gets_a_screenshot_event_after_its_action(self) -> None:
        trace = TraceEnvelope.from_agent_result(contract_hash="h", result=_result())

        self.assertEqual(
            [(e.step_idx, e.event_type) for e in trace.events], [(1, "action"), (1, "screenshot"), (2, "action")]
        )
        self.assertEqual(trace.events[1].payload["screenshot_path"], SHOT)
        self.assertIn("screenshots", trace.evidence)
        self.assertTrue(trace.verify_chain())

    def test_a_result_with_no_screenshots_adds_no_screenshot_events(self) -> None:
        result = _result()
        for step in result["steps"]:
            step["observation"] = {"url": "https://x.test/"}

        trace = TraceEnvelope.from_agent_result(contract_hash="h", result=result)

        self.assertEqual([e.event_type for e in trace.events], ["action", "action"])
        self.assertNotIn("screenshots", trace.evidence)
