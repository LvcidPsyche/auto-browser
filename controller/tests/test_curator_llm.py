from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.curator_llm import CuratorLLMAdapter, build_curator_adapter


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict, dict]] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def post(self, url: str, *, headers: dict, json: dict) -> _FakeResponse:
        self.calls.append((url, headers, json))
        return self.response


class CuratorLLMAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_from_env_returns_none_for_invalid_provider_or_missing_key(self) -> None:
        with patch.dict(os.environ, {"CURATOR_PROVIDER": "bogus"}, clear=False):
            self.assertIsNone(CuratorLLMAdapter.from_env())

        with patch.dict(os.environ, {"CURATOR_PROVIDER": "openai"}, clear=True):
            self.assertIsNone(CuratorLLMAdapter.from_env())

    async def test_from_env_missing_key_does_not_log_env_var_name(self) -> None:
        with (
            patch.dict(os.environ, {"CURATOR_PROVIDER": "openai"}, clear=True),
            self.assertLogs("app.curator_llm", level="INFO") as captured,
        ):
            self.assertIsNone(CuratorLLMAdapter.from_env())

        joined = "\n".join(captured.output)
        self.assertIn("provider=openai", joined)
        self.assertNotIn("OPENAI_API_KEY", joined)

    async def test_complete_builds_claude_request_and_extracts_text(self) -> None:
        fake_client = _FakeAsyncClient(_FakeResponse({"content": [{"type": "text", "text": "claude-output"}]}))
        adapter = CuratorLLMAdapter("claude", model="claude-test")

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "anthropic-key"}, clear=True),
            patch("app.curator_llm.httpx.AsyncClient", return_value=fake_client),
        ):
            result = await adapter.complete("summarize this", system="be concise")

        self.assertEqual(result, "claude-output")
        url, headers, body = fake_client.calls[0]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "anthropic-key")
        self.assertEqual(body["system"], "be concise")
        self.assertEqual(body["messages"][0]["content"], "summarize this")

    async def test_complete_builds_openai_request_and_extracts_text(self) -> None:
        fake_client = _FakeAsyncClient(_FakeResponse({"choices": [{"message": {"content": "openai-output"}}]}))
        adapter = CuratorLLMAdapter("openai", model="gpt-test")

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "openai-key"}, clear=True),
            patch("app.curator_llm.httpx.AsyncClient", return_value=fake_client),
        ):
            result = await adapter.complete("hello world", system="system note")

        self.assertEqual(result, "openai-output")
        url, headers, body = fake_client.calls[0]
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer openai-key")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "system note"})
        self.assertEqual(body["messages"][1], {"role": "user", "content": "hello world"})

    async def test_complete_builds_gemini_request_and_extracts_text(self) -> None:
        fake_client = _FakeAsyncClient(
            _FakeResponse({"candidates": [{"content": {"parts": [{"text": "gemini-output"}]}}]})
        )
        adapter = CuratorLLMAdapter("gemini", model="gemini-test")

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True),
            patch("app.curator_llm.httpx.AsyncClient", return_value=fake_client),
        ):
            result = await adapter.complete("idea list", system="use bullets")

        self.assertEqual(result, "gemini-output")
        url, headers, body = fake_client.calls[0]
        self.assertIn("gemini-test:generateContent?key=gemini-key", url)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "System: use bullets\n\n")
        self.assertEqual(body["contents"][0]["parts"][1]["text"], "idea list")

    async def test_complete_requires_api_key(self) -> None:
        adapter = CuratorLLMAdapter("openai")

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "no API key"):
                await adapter.complete("prompt")

    async def test_extract_text_returns_empty_for_malformed_payload(self) -> None:
        adapter = CuratorLLMAdapter("openai")

        self.assertEqual(adapter._extract_text({}), "")

    async def test_build_curator_adapter_swallows_factory_failures(self) -> None:
        with patch("app.curator_llm.CuratorLLMAdapter.from_env", side_effect=RuntimeError("boom")):
            self.assertIsNone(build_curator_adapter())
