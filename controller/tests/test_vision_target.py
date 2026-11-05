from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import Settings
from app.vision_target import VisionTargeter


def _make_targeter() -> VisionTargeter:
    return VisionTargeter(api_key="test-key", model="claude-haiku-4-5-20251001")


def _mock_async_client(response_payload: dict[str, object]) -> MagicMock:
    response = MagicMock()
    response.json.return_value = response_payload
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


class VisionTargeterAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)

    def _screenshot_path(self) -> Path:
        path = self.tmp_path / "shot.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        return path

    async def test_find_element_success(self) -> None:
        targeter = _make_targeter()
        shot = self._screenshot_path()
        response_payload = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "x": 100,
                            "y": 200,
                            "found": True,
                            "confidence": 0.95,
                            "description": "Blue submit button",
                        }
                    ),
                }
            ]
        }

        with patch("app.vision_target.httpx.AsyncClient", return_value=_mock_async_client(response_payload)):
            result = await targeter.find_element(str(shot), "the blue submit button")

        self.assertTrue(result["found"])
        self.assertEqual(result["x"], 100)
        self.assertEqual(result["y"], 200)

    async def test_find_element_not_found(self) -> None:
        targeter = _make_targeter()
        shot = self._screenshot_path()
        response_payload = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "x": 0,
                            "y": 0,
                            "found": False,
                            "confidence": 0.0,
                            "description": "not visible",
                        }
                    ),
                }
            ]
        }

        with patch("app.vision_target.httpx.AsyncClient", return_value=_mock_async_client(response_payload)):
            result = await targeter.find_element(str(shot), "invisible element")

        self.assertFalse(result["found"])

    async def test_find_element_malformed_json(self) -> None:
        targeter = _make_targeter()
        shot = self._screenshot_path()
        response_payload = {"content": [{"type": "text", "text": "not json at all"}]}

        with patch("app.vision_target.httpx.AsyncClient", return_value=_mock_async_client(response_payload)):
            result = await targeter.find_element(str(shot), "something")

        self.assertFalse(result["found"])
        self.assertIn("parse error", result["description"])

    async def test_default_settings_post_to_the_messages_url(self) -> None:
        # The default ANTHROPIC_BASE_URL already ends in /v1, as claude_adapter assumes.
        targeter = VisionTargeter.from_settings(Settings(_env_file=None, ANTHROPIC_API_KEY="k"))
        assert targeter is not None
        shot = self._screenshot_path()
        payload = {"content": [{"type": "text", "text": json.dumps({"x": 1, "y": 2, "found": True})}]}
        context = _mock_async_client(payload)

        with patch("app.vision_target.httpx.AsyncClient", return_value=context):
            await targeter.find_element(str(shot), "a button")

        client = context.__aenter__.return_value
        self.assertEqual(client.post.call_args.args[0], "https://api.anthropic.com/v1/messages")

    async def test_find_element_missing_screenshot(self) -> None:
        targeter = _make_targeter()

        with self.assertRaises(FileNotFoundError):
            await targeter.find_element("/nonexistent/path.png", "anything")


class VisionTargeterSettingsTests(unittest.TestCase):
    def test_from_settings_no_key(self) -> None:
        settings = SimpleNamespace(anthropic_api_key=None)

        result = VisionTargeter.from_settings(settings)

        self.assertIsNone(result)

    def test_from_settings_defaults_to_vision_model(self) -> None:
        settings = SimpleNamespace(
            anthropic_api_key="sk-test",
            anthropic_base_url="https://api.anthropic.com/v1",
            vision_model="claude-haiku-4-5-20251001",
        )

        result = VisionTargeter.from_settings(settings)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.model, "claude-haiku-4-5-20251001")

    def test_from_settings_uses_default_model_when_vision_model_missing(self) -> None:
        settings = SimpleNamespace(
            anthropic_api_key="sk-test",
            anthropic_base_url="https://api.anthropic.com/v1",
        )

        result = VisionTargeter.from_settings(settings)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.model, "claude-haiku-4-5-20251001")
