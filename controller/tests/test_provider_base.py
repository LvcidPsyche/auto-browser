"""Unit coverage for BaseProviderAdapter pure helpers.

No live browsers, no network, no provider credentials — every method exercised
here is deterministic string/JSON/filesystem logic on the shared adapter base.
OpenAIAdapter is used only as a concrete instantiation of the abstract base.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import httpx

from app.config import Settings
from app.models import BrowserActionDecision
from app.providers.base import BaseProviderAdapter
from app.providers.openai_adapter import OpenAIAdapter


def _decision_json(element_id: str = "e1") -> str:
    return (
        '{"action": "click", "reason": "advance the task", '
        f'"element_id": "{element_id}", "risk_category": "write"}}'
    )


class ProviderReadinessHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OpenAIAdapter(Settings(_env_file=None))

    def test_describe_api_readiness_reports_key_state(self) -> None:
        ready, detail = BaseProviderAdapter.describe_api_readiness(api_key="sk-x", env_var="OPENAI_API_KEY")
        self.assertTrue(ready)
        self.assertIn("OPENAI_API_KEY", detail)

        missing, detail = BaseProviderAdapter.describe_api_readiness(api_key=None, env_var="OPENAI_API_KEY")
        self.assertFalse(missing)
        self.assertIn("not configured", detail)

    def test_describe_socket_readiness_negative_branches(self) -> None:
        no_path = BaseProviderAdapter.describe_socket_readiness(socket_path=None, label="bridge")
        self.assertFalse(no_path[0])
        self.assertIn("not configured", no_path[1])

        missing = BaseProviderAdapter.describe_socket_readiness(
            socket_path="/nonexistent/socket.sock", label="bridge"
        )
        self.assertFalse(missing[0])
        self.assertIn("does not exist", missing[1])

        with tempfile.NamedTemporaryFile(suffix=".notsock") as handle:
            not_socket = BaseProviderAdapter.describe_socket_readiness(socket_path=handle.name, label="bridge")
        self.assertFalse(not_socket[0])
        self.assertIn("not a Unix socket", not_socket[1])

    def test_cli_binary_exists(self) -> None:
        self.assertFalse(BaseProviderAdapter.cli_binary_exists(None))
        self.assertFalse(BaseProviderAdapter.cli_binary_exists("definitely-not-a-real-binary-xyz"))
        self.assertTrue(BaseProviderAdapter.cli_binary_exists(sys.executable))

    def test_describe_cli_readiness_missing_binary(self) -> None:
        ready, detail = self.adapter.describe_cli_readiness(
            cli_path="definitely-not-a-real-binary-xyz",
            cli_label="codex",
            auth_markers=(".codex",),
        )
        self.assertFalse(ready)
        self.assertIn("was not found", detail)

    def test_auth_mode_helpers(self) -> None:
        self.assertEqual(BaseProviderAdapter.normalize_auth_mode("  API  "), "api")
        self.assertEqual(BaseProviderAdapter.normalize_auth_mode(None), "")
        self.assertIn("api", self.adapter.supported_auth_modes)
        self.assertTrue(self.adapter.auth_mode_supported("api"))
        self.assertFalse(self.adapter.auth_mode_supported("carrier-pigeon"))
        detail = self.adapter.invalid_auth_mode_detail("carrier-pigeon")
        self.assertIn("carrier-pigeon", detail)
        self.assertIn("expected one of", detail)


class ProviderObservationPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OpenAIAdapter(Settings(_env_file=None))

    def test_compact_observation_projects_known_fields(self) -> None:
        observation = {
            "session": "s1",
            "url": "https://example.com",
            "title": "Example",
            "interactables": [
                {"element_id": "e1", "label": "Submit", "role": "button", "extra": "dropme"}
            ],
            "unexpected_top_level": "dropme",
        }
        compact = self.adapter.compact_observation(observation)
        self.assertEqual(compact["session"], "s1")
        self.assertEqual(compact["url"], "https://example.com")
        self.assertEqual(compact["interactables"][0]["element_id"], "e1")
        self.assertNotIn("extra", compact["interactables"][0])
        self.assertNotIn("unexpected_top_level", compact)
        # Missing collections default to empty lists rather than KeyError.
        self.assertEqual(compact["console_messages"], [])

    def test_build_text_prompt_includes_goal_and_observation(self) -> None:
        prompt = self.adapter.build_text_prompt(
            goal="find the login button",
            observation={"url": "https://example.com", "interactables": []},
            context_hints=None,
            previous_steps=[],
        )
        self.assertIn("find the login button", prompt)
        self.assertIn("https://example.com", prompt)
        self.assertIn("risk_category", prompt)

    def test_strict_action_schema_is_a_dict(self) -> None:
        schema = self.adapter.strict_action_schema
        self.assertIsInstance(schema, dict)
        self.assertTrue(schema)


class ProviderDecisionParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OpenAIAdapter(Settings(_env_file=None))

    def test_parse_direct_json(self) -> None:
        decision = self.adapter.parse_decision_text(_decision_json())
        self.assertEqual(decision.action, "click")
        self.assertEqual(decision.element_id, "e1")

    def test_parse_json_embedded_in_prose(self) -> None:
        text = f"Sure, here is the next step:\n{_decision_json('button-7')}\nLet me know if that helps."
        decision = self.adapter.parse_decision_text(text)
        self.assertEqual(decision.element_id, "button-7")

    def test_parse_nested_decision_object(self) -> None:
        text = '{"result": {"choice": ' + _decision_json("nested-1") + "}}"
        decision = self.adapter.parse_decision_text(text)
        self.assertEqual(decision.element_id, "nested-1")

    def test_parse_empty_text_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self.adapter.parse_decision_text("   ")

    def test_parse_garbage_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self.adapter.parse_decision_text("no json here, just words {incomplete")

    def test_parse_decision_file_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"
            path.write_text(_decision_json("from-file"), encoding="utf-8")
            decision = self.adapter.parse_decision_file(path)
        self.assertEqual(decision.element_id, "from-file")

    def test_find_decision_candidate_variants(self) -> None:
        import json

        payload = json.loads(_decision_json("in-list"))
        self.assertIsNotNone(BaseProviderAdapter._find_decision_candidate([{"noise": 1}, payload]))
        self.assertIsNone(BaseProviderAdapter._find_decision_candidate({"nothing": "useful"}))
        self.assertIsNone(BaseProviderAdapter._find_decision_candidate(["a", "b"]))


class ProviderErrorExtractionTests(unittest.TestCase):
    def test_safe_json(self) -> None:
        ok = BaseProviderAdapter._safe_json(httpx.Response(200, json={"a": 1}))
        self.assertEqual(ok, {"a": 1})
        non_dict = BaseProviderAdapter._safe_json(httpx.Response(200, json=[1, 2]))
        self.assertIsNone(non_dict)
        invalid = BaseProviderAdapter._safe_json(httpx.Response(200, text="not json"))
        self.assertIsNone(invalid)

    def test_extract_error_message(self) -> None:
        self.assertIsNone(BaseProviderAdapter._extract_error_message(None))
        self.assertIsNone(BaseProviderAdapter._extract_error_message({}))
        structured = BaseProviderAdapter._extract_error_message(
            {"error": {"message": "bad key", "type": "auth", "code": "401"}}
        )
        self.assertIn("bad key", structured)
        self.assertIn("401", structured)
        flat = BaseProviderAdapter._extract_error_message({"message": "flat error"})
        self.assertEqual(flat, "flat error")


if __name__ == "__main__":
    unittest.main()
