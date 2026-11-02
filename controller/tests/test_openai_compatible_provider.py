from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.config import Settings
from app.provider_registry import ProviderRegistry
from app.providers.base import ProviderAPIError
from app.providers.openai_compatible import (
    OPENAI_COMPATIBLE_PROFILES,
    OpenAICompatibleAdapter,
)

# Smallest valid PNG so encode_image() has a real file to read for vision providers.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001a5f645400000000049454e44ae426082"
)


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeAsyncClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


class FakeSequenceClient:
    """Answers each post with the next queued response and keeps a snapshot of every payload."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def post(self, url, headers=None, json=None):
        # A snapshot, so a payload the adapter edits after sending still shows what went out.
        self.calls.append({"url": url, "headers": headers, "json": copy.deepcopy(json)})
        return self.responses.pop(0)


_NAMED_TOOL_CHOICE = {"type": "function", "function": {"name": "browser_action"}}
_DONE = {"action": "done", "reason": "complete"}


def _http_error(body: dict, status_code: int = 400) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request("POST", "https://example.com"), json=body)


def _provider_env_names() -> list[str]:
    """Env names of every provider key, base URL and model: whatever the developer exported."""
    attrs = [
        attr
        for profile in OPENAI_COMPATIBLE_PROFILES
        for attr in (profile.api_key_attr, profile.base_url_attr, profile.model_attr)
    ]
    return [Settings.model_fields[attr].alias for attr in attrs]


def _settings(**overrides) -> Settings:
    # _env_file=None only skips .env; a real OPENROUTER_API_KEY in the shell still
    # reached Settings and made "not configured" tests fail on developer machines.
    with patch.dict(os.environ):
        for name in _provider_env_names():
            os.environ.pop(name, None)
        settings = Settings(_env_file=None)
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _tool_call_response(arguments: dict) -> FakeResponse:
    return FakeResponse(
        {
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "tool_calls": [{"function": {"name": "browser_action", "arguments": json.dumps(arguments)}}]
                    }
                }
            ],
            "usage": {"total_tokens": 5},
        }
    )


def _content_response(text: str) -> FakeResponse:
    return FakeResponse({"model": "test-model", "choices": [{"message": {"content": text}}]})


class OpenAICompatibleProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.image = Path(self.tempdir.name) / "shot.png"
        self.image.write_bytes(_PNG)

    def _observation(self) -> dict:
        return {"screenshot_path": str(self.image), "url": "https://example.com", "title": "Example"}

    def _adapter(self, provider: str, **cfg) -> OpenAICompatibleAdapter:
        profile = next(p for p in OPENAI_COMPATIBLE_PROFILES if p.provider == provider)
        return OpenAICompatibleAdapter(_settings(**cfg), profile)

    async def _decide_with(self, adapter: OpenAICompatibleAdapter, fake: FakeSequenceClient, **kwargs):
        with patch("app.providers.base.httpx.AsyncClient", return_value=fake):
            return await adapter.decide(goal="g", observation=self._observation(), **kwargs)

    def test_registry_registers_all_openai_compatible_providers(self):
        registry = ProviderRegistry(_settings())
        for name in ("openrouter", "xai", "deepseek", "minimax", "openai_compatible"):
            self.assertIn(name, registry.providers)
            self.assertEqual(registry.providers[name].provider, name)

    def test_not_configured_without_api_key(self):
        adapter = self._adapter("openrouter")
        self.assertFalse(adapter.configured)
        self.assertIn("OPENROUTER_API_KEY", adapter.readiness_detail)

    def test_configured_with_key_and_defaulted_base_and_model(self):
        adapter = self._adapter("xai", xai_api_key="k")
        self.assertTrue(adapter.configured)
        self.assertEqual(adapter.default_model, "grok-4")

    def test_custom_endpoint_reports_missing_base_url_and_model(self):
        adapter = self._adapter("openai_compatible", openai_compatible_api_key="k")
        self.assertFalse(adapter.configured)
        detail = adapter.readiness_detail
        self.assertIn("OPENAI_COMPATIBLE_BASE_URL", detail)
        self.assertIn("OPENAI_COMPATIBLE_MODEL", detail)

    async def test_decide_via_tool_call_sends_vision(self):
        adapter = self._adapter("minimax", minimax_api_key="k")
        fake = FakeAsyncClient(_tool_call_response({"action": "done", "reason": "complete"}))
        with patch("app.providers.base.httpx.AsyncClient", return_value=fake):
            decision = await adapter.decide(goal="g", observation=self._observation())
        self.assertEqual(decision.decision.action, "done")
        self.assertEqual(decision.provider, "minimax")
        sent = fake.calls[0]["json"]
        self.assertEqual(sent["model"], "MiniMax-M3")
        self.assertIn("image_url", [c["type"] for c in sent["messages"][1]["content"]])
        self.assertEqual(fake.calls[0]["url"], "https://api.minimax.io/v1/chat/completions")

    async def test_decide_falls_back_to_content_when_no_tool_call(self):
        adapter = self._adapter("openrouter", openrouter_api_key="k", openrouter_model="anthropic/claude-3.7-sonnet")
        fake = FakeAsyncClient(_content_response(json.dumps({"action": "done", "reason": "ok"})))
        with patch("app.providers.base.httpx.AsyncClient", return_value=fake):
            decision = await adapter.decide(goal="g", observation=self._observation())
        self.assertEqual(decision.decision.action, "done")

    async def test_empty_choices_raises_labeled_error(self):
        adapter = self._adapter("xai", xai_api_key="k")
        fake = FakeAsyncClient(FakeResponse({"model": "test-model", "choices": []}))
        with patch("app.providers.base.httpx.AsyncClient", return_value=fake):
            with self.assertRaises(RuntimeError) as ctx:
                await adapter.decide(goal="g", observation=self._observation())
        self.assertIn("xAI Grok", str(ctx.exception))

    async def test_missing_choices_key_raises_labeled_error(self):
        adapter = self._adapter("openrouter", openrouter_api_key="k", openrouter_model="m")
        fake = FakeAsyncClient(FakeResponse({"error": {"message": "overloaded"}}))
        with patch("app.providers.base.httpx.AsyncClient", return_value=fake):
            with self.assertRaises(RuntimeError) as ctx:
                await adapter.decide(goal="g", observation=self._observation())
        self.assertIn("OpenRouter", str(ctx.exception))

    async def test_text_only_provider_omits_image(self):
        adapter = self._adapter("deepseek", deepseek_api_key="k")
        fake = FakeAsyncClient(_tool_call_response({"action": "done", "reason": "x"}))
        with patch("app.providers.base.httpx.AsyncClient", return_value=fake):
            await adapter.decide(goal="g", observation=self._observation())
        content_types = [c["type"] for c in fake.calls[0]["json"]["messages"][1]["content"]]
        self.assertNotIn("image_url", content_types)

    async def test_an_endpoint_that_accepts_the_named_choice_gets_it_on_every_step(self):
        adapter = self._adapter("xai", xai_api_key="k")
        fake = FakeSequenceClient([_tool_call_response(_DONE), _tool_call_response(_DONE)])
        decision = await self._decide_with(adapter, fake)
        await self._decide_with(adapter, fake)
        self.assertEqual(decision.decision.action, "done")
        self.assertEqual(len(fake.calls), 2)
        for call in fake.calls:
            self.assertEqual(call["json"]["tool_choice"], _NAMED_TOOL_CHOICE)

    async def test_thinking_mode_400_retries_once_without_tool_choice(self):
        adapter = self._adapter("deepseek", deepseek_api_key="k")
        fake = FakeSequenceClient(
            [
                _http_error(
                    {
                        "error": {
                            "message": "Thinking mode does not support this tool_choice",
                            "type": "invalid_request_error",
                        }
                    }
                ),
                _tool_call_response(_DONE),
            ]
        )
        with self.assertLogs("app.providers.openai_compatible", level="INFO") as logs:
            decision = await self._decide_with(adapter, fake)
        self.assertEqual(decision.decision.action, "done")
        self.assertEqual(len(fake.calls), 2)
        first, second = fake.calls[0]["json"], fake.calls[1]["json"]
        self.assertEqual(first["tool_choice"], _NAMED_TOOL_CHOICE)
        self.assertNotIn("tool_choice", second)
        self.assertEqual({k: v for k, v in first.items() if k != "tool_choice"}, second)
        self.assertEqual(fake.calls[0]["url"], fake.calls[1]["url"])
        self.assertEqual(fake.calls[0]["headers"], fake.calls[1]["headers"])
        [line] = logs.output
        self.assertIn("deepseek-chat", line)
        self.assertNotIn("api.deepseek.com", line)

    async def test_other_spellings_and_plain_text_bodies_naming_tool_choice_fall_back(self):
        bodies = {
            # DeepSeek's API reference words it this way.
            "docs prose": {"error": {"message": "`required` and named tool choices are not supported in thinking mode"}},
            "capitalised": {"error": {"message": "Tool choice is not supported for this model"}},
            "upper case": {"error": {"message": "TOOL_CHOICE unsupported"}},
            "hyphen": {"error": {"message": "unsupported tool-choice value"}},
            "OpenAI param field": {"error": {"message": "Invalid value", "param": "tool_choice"}},
        }
        for name, body in bodies.items():
            with self.subTest(name):
                adapter = self._adapter("deepseek", deepseek_api_key="k")
                fake = FakeSequenceClient([_http_error(body), _tool_call_response(_DONE)])
                await self._decide_with(adapter, fake)
                self.assertEqual(len(fake.calls), 2)
                self.assertNotIn("tool_choice", fake.calls[1]["json"])
        with self.subTest("plain-text body"):
            adapter = self._adapter("deepseek", deepseek_api_key="k")
            plain = httpx.Response(
                400, request=httpx.Request("POST", "https://example.com"), text="tool_choice is not supported"
            )
            fake = FakeSequenceClient([plain, _tool_call_response(_DONE)])
            await self._decide_with(adapter, fake)
            self.assertEqual(len(fake.calls), 2)
            self.assertNotIn("tool_choice", fake.calls[1]["json"])

    async def test_openrouter_400_naming_tool_choice_only_in_metadata_falls_back(self):
        adapter = self._adapter("openrouter", openrouter_api_key="k", openrouter_model="anthropic/claude-opus-5.5")
        fake = FakeSequenceClient(
            [
                _http_error(
                    {
                        "error": {
                            "code": 400,
                            "message": "Provider returned error",
                            "metadata": {
                                "raw": 'tool_choice: type "tool" and "any" are not supported for this model.',
                                "provider_name": "Anthropic",
                            },
                        }
                    }
                ),
                _tool_call_response(_DONE),
            ]
        )
        decision = await self._decide_with(adapter, fake)
        self.assertEqual(decision.decision.action, "done")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[0]["json"]["model"], "anthropic/claude-opus-5.5")
        self.assertEqual(fake.calls[0]["json"]["tool_choice"], _NAMED_TOOL_CHOICE)
        self.assertEqual(fake.calls[1]["json"]["model"], "anthropic/claude-opus-5.5")
        self.assertNotIn("tool_choice", fake.calls[1]["json"])

    async def test_fallback_is_remembered_for_that_model_only(self):
        adapter = self._adapter("deepseek", deepseek_api_key="k")
        fake = FakeSequenceClient(
            [
                _http_error({"error": {"message": "Thinking mode does not support this tool_choice"}}),
                _tool_call_response(_DONE),
                _tool_call_response(_DONE),
                _tool_call_response(_DONE),
            ]
        )
        await self._decide_with(adapter, fake)
        self.assertEqual(len(fake.calls), 2)

        await self._decide_with(adapter, fake)
        self.assertEqual(len(fake.calls), 3)
        self.assertNotIn("tool_choice", fake.calls[2]["json"])

        await self._decide_with(adapter, fake, model_override="other-model")
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(fake.calls[3]["json"]["model"], "other-model")
        self.assertEqual(fake.calls[3]["json"]["tool_choice"], _NAMED_TOOL_CHOICE)

    async def test_fallback_is_remembered_for_that_base_url_only(self):
        adapter = self._adapter("deepseek", deepseek_api_key="k")
        fake = FakeSequenceClient(
            [
                _http_error({"error": {"message": "Thinking mode does not support this tool_choice"}}),
                _tool_call_response(_DONE),
                _tool_call_response(_DONE),
            ]
        )
        await self._decide_with(adapter, fake)
        adapter.settings.deepseek_base_url = "https://gateway.example/v1"
        await self._decide_with(adapter, fake)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(fake.calls[2]["url"], "https://gateway.example/v1/chat/completions")
        self.assertEqual(fake.calls[2]["json"]["tool_choice"], _NAMED_TOOL_CHOICE)

    async def test_a_400_that_does_not_name_tool_choice_propagates_without_retry(self):
        messages = [
            "context length exceeded",
            "tools: invalid schema",
            # The strict-schema 400 OpenAI gave before the strict form was adopted.
            'Invalid schema for function browser_action: "required" must list every property',
            "n must be 1: only one choice is supported",
        ]
        for message in messages:
            with self.subTest(message):
                adapter = self._adapter("deepseek", deepseek_api_key="k")
                # A second response a wrongful retry would take and succeed on.
                fake = FakeSequenceClient([_http_error({"error": {"message": message}}), _tool_call_response(_DONE)])
                with self.assertRaises(ProviderAPIError) as ctx:
                    await self._decide_with(adapter, fake)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn(message, ctx.exception.message)
                self.assertEqual(len(fake.calls), 1)

    async def test_tool_choice_error_with_another_status_propagates_without_retry(self):
        for status_code in (422, 500):
            with self.subTest(status_code):
                # No 5xx retries, so a single request is all _post_json makes.
                adapter = self._adapter(
                    "deepseek", deepseek_api_key="k", model_max_retries=0, model_retry_backoff_seconds=0
                )
                fake = FakeSequenceClient(
                    [
                        _http_error({"error": {"message": "tool_choice is not supported"}}, status_code=status_code),
                        _tool_call_response(_DONE),
                    ]
                )
                with self.assertRaises(ProviderAPIError) as ctx:
                    await self._decide_with(adapter, fake)
                self.assertEqual(ctx.exception.status_code, status_code)
                self.assertEqual(len(fake.calls), 1)

    async def test_an_endpoint_refusing_both_forms_costs_two_requests_and_remembers_nothing(self):
        # vLLM 0.14 and later started without --enable-auto-tool-choice and --tool-call-parser.
        adapter = self._adapter(
            "openai_compatible",
            openai_compatible_api_key="k",
            openai_compatible_base_url="http://vllm:8000/v1",
            openai_compatible_model="m",
        )
        named_400 = {"error": {"message": 'tool_choice=function "browser_action" requires --tool-call-parser to be set'}}
        auto_400 = {
            "error": {"message": '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set'}
        }
        fake = FakeSequenceClient([_http_error(named_400), _http_error(auto_400), _tool_call_response(_DONE)])
        with self.assertRaises(ProviderAPIError) as ctx:
            await self._decide_with(adapter, fake)
        self.assertIn('"auto" tool choice requires', ctx.exception.message)
        self.assertEqual(len(fake.calls), 2)
        await self._decide_with(adapter, fake)
        self.assertEqual(fake.calls[2]["json"]["tool_choice"], _NAMED_TOOL_CHOICE)

    async def test_failed_retry_raises_its_error_and_remembers_nothing(self):
        adapter = self._adapter("deepseek", deepseek_api_key="k")
        fake = FakeSequenceClient(
            [
                _http_error({"error": {"message": "Thinking mode does not support this tool_choice"}}),
                _http_error({"error": {"message": "tools: invalid schema"}}),
                _tool_call_response(_DONE),
            ]
        )
        with self.assertRaises(ProviderAPIError) as ctx:
            await self._decide_with(adapter, fake)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("tools: invalid schema", ctx.exception.message)
        self.assertEqual(len(fake.calls), 2)
        self.assertNotIn("tool_choice", fake.calls[1]["json"])

        await self._decide_with(adapter, fake)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(fake.calls[2]["json"]["tool_choice"], _NAMED_TOOL_CHOICE)

    async def test_a_remembered_endpoint_is_not_retried_on_a_later_tool_choice_400(self):
        adapter = self._adapter("deepseek", deepseek_api_key="k")
        fake = FakeSequenceClient(
            [
                _http_error({"error": {"message": "Thinking mode does not support this tool_choice"}}),
                _tool_call_response(_DONE),
                _http_error({"error": {"message": "tool_choice is not supported"}}),
                _tool_call_response(_DONE),
            ]
        )
        await self._decide_with(adapter, fake)
        with self.assertRaises(ProviderAPIError):
            await self._decide_with(adapter, fake)
        self.assertEqual(len(fake.calls), 3)
        self.assertNotIn("tool_choice", fake.calls[2]["json"])


if __name__ == "__main__":
    unittest.main()
