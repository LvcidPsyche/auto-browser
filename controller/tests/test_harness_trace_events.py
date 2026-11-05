"""TraceEnvelope.from_agent_result carries the screenshots a live agent result holds, not only its actions."""

from __future__ import annotations

import tempfile
import unittest

from app.harness import Budget, Postcondition, TaskContract
from app.harness.iterate import HarnessService
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

    def test_a_sensitive_decision_hides_its_value_and_username_in_both_events(self) -> None:
        account, user = "ACCT-99812", "jdoe-admin"
        decision = {"action": "select_option", "selector": "#acct", "value": account, "username": user}
        result = {
            "steps": [
                {
                    "status": "ok",
                    "decision": {**decision, "sensitive": True},
                    "observation": {"url": "https://x.test/"},
                    "execution": {},
                }
            ]
        }

        trace = TraceEnvelope.from_agent_result(contract_hash="h", result=result)

        for event_type in ("model_decision", "action"):
            stored = next(e for e in trace.events if e.event_type == event_type).payload["decision"]
            self.assertEqual((stored["value"], stored["username"]), ("[REDACTED]", "[REDACTED]"), event_type)
            self.assertEqual(stored["selector"], "#acct")
        self.assertNotIn(account, trace.model_dump_json())
        self.assertNotIn(user, trace.model_dump_json())

    def test_a_sensitive_select_option_hides_its_choice_in_the_executions_target_too(self) -> None:
        # The real execution shape: the action's target is {**target, value, label, index} with no sensitive flag of
        # its own, so the decision's flag has to reach it.
        account, label = "ACCT-99812", "Savings 9812"
        execution = {
            "target": {"selector": "#acct", "value": account, "label": label, "index": None},
            "before": {"url": "https://x.test/"},
            "after": {"url": "https://x.test/done"},
        }
        decision = {"action": "select_option", "selector": "#acct", "value": account, "label": label}
        for name, step_decision, step_execution in (
            ("declared", {**decision, "sensitive": True}, execution),
            (
                "found at run time",
                decision,
                {**execution, "target": {**execution["target"], "text_redacted": True}},
            ),
        ):
            with self.subTest(sensitive=name):
                result = {"steps": [{"status": "ok", "decision": step_decision, "execution": step_execution}]}

                trace = TraceEnvelope.from_agent_result(contract_hash="h", result=result)

                stored = trace.model_dump_json()
                self.assertNotIn(account, stored)
                self.assertNotIn(label, stored)
                action = next(e for e in trace.events if e.event_type == "action").payload
                self.assertEqual(action["execution"]["target"]["selector"], "#acct")
                self.assertEqual(action["execution"]["after"]["url"], "https://x.test/done")

    def test_an_ordinary_selects_target_keeps_its_value_and_label(self) -> None:
        execution = {"target": {"selector": "#size", "value": "m", "label": "Medium"}}
        result = {"steps": [{"status": "ok", "decision": {"action": "select_option"}, "execution": execution}]}

        trace = TraceEnvelope.from_agent_result(contract_hash="h", result=result)

        target = next(e for e in trace.events if e.event_type == "action").payload["execution"]["target"]
        self.assertEqual((target["value"], target["label"]), ("m", "Medium"))

    def test_an_ordinary_decision_keeps_its_value(self) -> None:
        result = {"steps": [{"status": "ok", "decision": {"action": "select_option", "value": "blue"}}]}

        trace = TraceEnvelope.from_agent_result(contract_hash="h", result=result)

        self.assertEqual(next(e for e in trace.events if e.event_type == "action").payload["decision"]["value"], "blue")

    def test_the_after_action_screenshot_is_a_second_screenshot_event_after_the_action(self) -> None:
        after = {"url": "https://x.test/done", "screenshot_path": "/data/artifacts/s1/after-click.png"}
        result = _result()
        result["steps"][0]["execution"] = {"after": after}
        result["steps"][1]["execution"] = {"after": {"url": "https://x.test/b", "screenshot_path": None}}

        trace = TraceEnvelope.from_agent_result(contract_hash="h", result=result)

        self.assertEqual(
            [(e.step_idx, e.event_type) for e in trace.events],
            [
                (1, "screenshot"),
                (1, "model_decision"),
                (1, "action"),
                (1, "screenshot"),
                (2, "model_decision"),
                (2, "action"),
            ],
        )
        self.assertEqual(trace.events[3].payload, {"screenshot_path": after["screenshot_path"]})
        self.assertTrue(trace.verify_chain())

    def test_an_after_screenshot_alone_meets_a_screenshots_requirement(self) -> None:
        result = {"steps": [{"status": "ok", "decision": {"action": "click"}, "observation": {}}]}
        result["steps"][0]["execution"] = {"after": {"screenshot_url": "/artifacts/s1/after-click.png"}}

        trace = TraceEnvelope.from_agent_result(contract_hash="h", result=result)

        self.assertIn("screenshots", trace.evidence)


class MockAttemptEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_mock_attempts_decision_and_action_say_they_are_simulated(self) -> None:
        contract = TaskContract(
            id="mock-events",
            goal="Open the page.",
            postconditions=[Postcondition(kind="url_contains", value="x.test/done")],
            budget=Budget(max_attempts=1, max_steps=2),
        )
        with tempfile.TemporaryDirectory() as tmp:
            service = HarnessService(tmp)
            record = await service.start_convergence(contract, mock_final_observation={"url": "https://x.test/done"})

            events = service.get_trace(record.id)["events"]

        simulated = {event["event_type"]: event["payload"].get("simulated") for event in events}
        self.assertEqual(simulated, {"model_decision": True, "action": True, "screenshot": None})


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
