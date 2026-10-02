"""The OpenAI adapters send requests OpenAI accepts.

Measured against the live API on 2026-10-01: the OpenAI adapter's request was
rejected on every step. It sent the plain action schema with `strict: true`
(400: "required" must list every property) and `temperature: 0` (400 on GPT-5
and later models), and current models such as gpt-6.1-sol refuse function
tools on chat completions altogether while they reason. The adapter now uses
the Responses API with the strict schema; the OpenAI-compatible adapter keeps
chat completions but sends the strict schema and no temperature.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image

from app.config import Settings
from app.providers.openai_adapter import OpenAIAdapter
from app.providers.openai_compatible import OPENAI_COMPATIBLE_PROFILES, OpenAICompatibleAdapter

ARGUMENTS = json.dumps({"action": "click", "reason": "open the menu", "element_id": "op-1"})


def _strict_violations(node, path: str = "$") -> list[str]:
    """OpenAI's strict-mode rules for function parameters, as the API enforced them."""
    problems: list[str] = []
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            if node.get("additionalProperties") is not False:
                problems.append(f"{path}: additionalProperties must be false")
            if sorted(node.get("required") or []) != sorted(properties):
                problems.append(f"{path}: required must list every property")
        if "default" in node:
            problems.append(f"{path}: default is not allowed")
        for key, value in node.items():
            problems.extend(_strict_violations(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            problems.extend(_strict_violations(value, f"{path}[{index}]"))
    return problems


class OpenAIAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.shot = Path(self.tempdir.name) / "shot.png"
        Image.new("RGB", (8, 8), "white").save(self.shot)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def _decide(self, response: dict):
        adapter = OpenAIAdapter(Settings(_env_file=None, OPENAI_API_KEY="test-key"))
        adapter._post_json = AsyncMock(return_value=response)  # type: ignore[method-assign]
        decision = await adapter._decide(
            goal="Open the menu",
            observation={"screenshot_path": str(self.shot), "url": "https://example.com", "title": "Example"},
            context_hints=None,
            previous_steps=[],
            model_override=None,
        )
        return adapter, decision, adapter._post_json.await_args.kwargs

    async def test_the_request_is_a_responses_call_with_the_strict_schema(self) -> None:
        response = {
            "model": "gpt-6.1-sol",
            "status": "completed",
            "output": [{"type": "function_call", "arguments": ARGUMENTS}],
        }
        adapter, decision, sent = await self._decide(response)
        payload = sent["payload"]

        self.assertTrue(sent["url"].endswith("/responses"), sent["url"])
        self.assertEqual(payload["model"], "gpt-6.1-sol")
        self.assertNotIn("temperature", payload)
        tool = payload["tools"][0]
        self.assertEqual((tool["name"], tool["strict"]), ("browser_action", True))
        self.assertEqual(tool["parameters"], adapter.strict_action_schema)
        self.assertEqual(payload["tool_choice"], {"type": "function", "name": "browser_action"})
        self.assertEqual([part["type"] for part in payload["input"][0]["content"]], ["input_text", "input_image"])
        self.assertEqual(decision.decision.element_id, "op-1")
        self.assertEqual(decision.raw_text, ARGUMENTS)

    async def test_the_function_call_is_found_after_reasoning_items(self) -> None:
        response = {
            "status": "completed",
            "output": [{"type": "reasoning", "summary": []}, {"type": "function_call", "arguments": ARGUMENTS}],
            "usage": {"input_tokens": 600, "output_tokens": 40, "total_tokens": 640},
        }
        _adapter, decision, _sent = await self._decide(response)
        self.assertEqual(decision.decision.action, "click")
        self.assertEqual(decision.usage["total_tokens"], 640)

    async def test_a_response_without_the_call_says_why(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            await self._decide({"status": "incomplete", "output": [{"type": "message", "content": []}]})
        self.assertIn("incomplete", str(ctx.exception))

    def test_the_strict_schema_meets_openai_strict_rules(self) -> None:
        schema = OpenAIAdapter(Settings(_env_file=None)).strict_action_schema
        self.assertEqual(_strict_violations(schema), [])
        # Guard the guard: the plain schema is what OpenAI rejected.
        self.assertNotEqual(_strict_violations(OpenAIAdapter(Settings(_env_file=None)).action_schema), [])


class OpenAICompatibleAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_payload_carries_the_strict_schema_and_no_temperature(self) -> None:
        profile = next(p for p in OPENAI_COMPATIBLE_PROFILES if p.provider == "openrouter")
        adapter = OpenAICompatibleAdapter(Settings(_env_file=None, OPENROUTER_API_KEY="test-key"), profile)
        tool_call = {"function": {"name": "browser_action", "arguments": ARGUMENTS}}
        adapter._post_json = AsyncMock(return_value={"choices": [{"message": {"tool_calls": [tool_call]}}]})  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "shot.png"
            Image.new("RGB", (8, 8), "white").save(shot)
            await adapter._decide(
                goal="Open the menu",
                observation={"screenshot_path": str(shot), "url": "https://example.com", "title": "Example"},
                context_hints=None,
                previous_steps=[],
                model_override=None,
            )
        payload = adapter._post_json.await_args.kwargs["payload"]
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["tools"][0]["function"]["parameters"], adapter.strict_action_schema)


if __name__ == "__main__":
    unittest.main()
