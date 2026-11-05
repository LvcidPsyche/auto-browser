"""One model-name pattern for provider_model, and the Gemini URL it is spliced into.

STORE-29: provider_model was any string, and the Gemini adapter put it into the request path unquoted, so
`x?key=...#` or `../` rewrote the URL the API key was sent to.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock

from app.config import Settings
from app.models import MODEL_NAME_PATTERN, AgentStepRequest
from app.providers.gemini_adapter import GeminiAdapter

_GOOD = ("gpt-6.1-sol", "claude-sonnet-4-6", "gemini-3.5-flash", "models/gemini-3.5-flash", "llama3:8b", "a")
_BAD = (
    "",
    "-rf",
    "--model",
    "a/../b",
    "..",
    "a..b",
    "x?key=1",
    "x y",
    "x#frag",
    "a" * 129,
    "/abs",
    "m;ls",
)


class ProviderModelPatternTests(unittest.TestCase):
    def test_good_names_are_accepted(self):
        for name in _GOOD:
            with self.subTest(name=name):
                self.assertEqual(
                    AgentStepRequest(provider="openai", goal="g", provider_model=name).provider_model, name
                )

    def test_bad_names_are_refused(self):
        for name in _BAD:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    AgentStepRequest(provider="openai", goal="g", provider_model=name)

    def test_provider_model_stays_optional(self):
        self.assertIsNone(AgentStepRequest(provider="openai", goal="g").provider_model)

    def test_the_pattern_is_the_documented_one(self):
        self.assertEqual(MODEL_NAME_PATTERN, r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class GeminiUrlQuotingTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_model_is_quoted_into_the_request_path(self):
        adapter = GeminiAdapter(Settings(_env_file=None, GEMINI_API_KEY="k"))
        adapter._post_json = AsyncMock(return_value={})
        observation: dict[str, Any] = {"url": "https://example.com", "title": "t", "text_excerpt": "", "preset": "text"}
        try:
            await adapter._decide(
                goal="g",
                observation=observation,
                context_hints=None,
                previous_steps=[],
                model_override="evil?x=1#frag",
            )
        except RuntimeError:
            pass
        url = adapter._post_json.await_args.kwargs["url"]
        self.assertTrue(url.endswith("/models/evil%3Fx%3D1%23frag:generateContent"), url)

    async def test_a_normal_model_keeps_its_slashes_and_colon(self):
        adapter = GeminiAdapter(Settings(_env_file=None, GEMINI_API_KEY="k"))
        adapter._post_json = AsyncMock(return_value={})
        try:
            await adapter._decide(
                goal="g",
                observation={"url": "u", "title": "t", "text_excerpt": "", "preset": "text"},
                context_hints=None,
                previous_steps=[],
                model_override="tunedModels/my-model:v1",
            )
        except RuntimeError:
            pass
        url = adapter._post_json.await_args.kwargs["url"]
        self.assertTrue(url.endswith("/models/tunedModels/my-model:v1:generateContent"), url)


if __name__ == "__main__":
    unittest.main()
