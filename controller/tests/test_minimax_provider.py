from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.config import Settings
from app.provider_registry import ProviderRegistry
from app.providers.minimax_adapter import MinimaxAdapter


class FakeAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class MinimaxAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.screenshot_path = root / "screen.png"
        self.screenshot_path.write_bytes(b"fake")
        self.observation = {
            "screenshot_path": str(self.screenshot_path),
            "url": "https://example.com",
            "title": "Example",
            "interactables": [],
        }

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_api_mode_returns_tool_call_decision(self) -> None:
        settings = Settings(_env_file=None, MINIMAX_API_KEY="test-key")
        adapter = MinimaxAdapter(settings)

        request = httpx.Request("POST", "https://api.minimax.io/v1/chat/completions")
        responses = [
            httpx.Response(
                200,
                request=request,
                json={
                    "model": "MiniMax-M3",
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "browser_action",
                                            "arguments": '{"action":"done","reason":"complete","risk_category":"read"}',
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                    "usage": {"total_tokens": 12},
                },
            )
        ]
        fake = FakeAsyncClient(responses)

        with patch("app.providers.base.httpx.AsyncClient", return_value=fake):
            result = await adapter._decide(
                goal="Finish the task",
                observation=self.observation,
                context_hints=None,
                previous_steps=[],
                model_override=None,
            )

        self.assertEqual(result.provider, "minimax")
        self.assertEqual(result.model, "MiniMax-M3")
        self.assertEqual(result.decision.action, "done")
        self.assertEqual(fake.requests[0]["url"], "https://api.minimax.io/v1/chat/completions")
        self.assertEqual(fake.requests[0]["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(fake.requests[0]["json"]["model"], "MiniMax-M3")

    def test_missing_api_key_is_not_configured(self) -> None:
        adapter = MinimaxAdapter(Settings(_env_file=None))
        self.assertFalse(adapter.configured)
        self.assertIn("MINIMAX_API_KEY", adapter.readiness_detail)

    def test_api_key_present_is_configured(self) -> None:
        adapter = MinimaxAdapter(Settings(_env_file=None, MINIMAX_API_KEY="test-key"))
        self.assertTrue(adapter.configured)
        self.assertEqual(adapter.default_model, "MiniMax-M3")

    def test_registry_exposes_minimax_provider(self) -> None:
        infos = {item.provider: item for item in ProviderRegistry(Settings(_env_file=None)).list()}
        self.assertIn("minimax", infos)
        self.assertEqual(infos["minimax"].model, "MiniMax-M3")


if __name__ == "__main__":
    unittest.main()
