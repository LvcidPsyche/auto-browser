from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.provider_registry import ProviderRegistry
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


def _settings(**overrides) -> Settings:
    settings = Settings(_env_file=None)
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _tool_call_response(arguments: dict) -> FakeResponse:
    return FakeResponse(
        {
            "model": "test-model",
            "choices": [
                {"message": {"tool_calls": [{"function": {"name": "browser_action", "arguments": json.dumps(arguments)}}]}}
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
        adapter = self._adapter(
            "openrouter", openrouter_api_key="k", openrouter_model="anthropic/claude-3.7-sonnet"
        )
        fake = FakeAsyncClient(_content_response(json.dumps({"action": "done", "reason": "ok"})))
        with patch("app.providers.base.httpx.AsyncClient", return_value=fake):
            decision = await adapter.decide(goal="g", observation=self._observation())
        self.assertEqual(decision.decision.action, "done")

    async def test_text_only_provider_omits_image(self):
        adapter = self._adapter("deepseek", deepseek_api_key="k")
        fake = FakeAsyncClient(_tool_call_response({"action": "done", "reason": "x"}))
        with patch("app.providers.base.httpx.AsyncClient", return_value=fake):
            await adapter.decide(goal="g", observation=self._observation())
        content_types = [c["type"] for c in fake.calls[0]["json"]["messages"][1]["content"]]
        self.assertNotIn("image_url", content_types)


if __name__ == "__main__":
    unittest.main()
