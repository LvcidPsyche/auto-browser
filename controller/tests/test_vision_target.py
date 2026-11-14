from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from app.config import Settings
from app.models import McpToolCallRequest
from app.tool_gateway import McpToolGateway
from app.vision_target import VisionReplyError, VisionTargeter

SCREENSHOT_SIZE = (1280, 800)


def _make_targeter() -> VisionTargeter:
    return VisionTargeter(api_key="test-key", model="claude-haiku-4-5-20251001")


def _reply(text: str) -> dict[str, object]:
    return {"content": [{"type": "text", "text": text}]}


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
        Image.new("RGB", SCREENSHOT_SIZE).save(path)
        return path

    async def _find(self, reply_text: str, shot: Path | None = None) -> dict[str, object]:
        with patch("app.vision_target.httpx.AsyncClient", return_value=_mock_async_client(_reply(reply_text))):
            return await _make_targeter().find_element(str(shot or self._screenshot_path()), "the button")

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

    async def test_a_reply_that_is_not_a_json_object_raises_the_named_error(self) -> None:
        for reply in ("not json at all", "[1, 2]", '"just a string"', "7", "null"):
            with self.subTest(reply=reply), self.assertRaises(VisionReplyError):
                await self._find(reply)

    async def test_a_reply_without_found_raises_the_named_error(self) -> None:
        # The prompt requires the key, so a reply object without it is no more an answer than a non-boolean found.
        for reply in ("{}", '{"x": 3, "y": 4, "confidence": 0.9}'):
            with self.subTest(reply=reply):
                with self.assertRaises(VisionReplyError) as raised:
                    await self._find(reply)
                self.assertIn("missing", str(raised.exception))

    async def test_a_found_reply_with_a_bad_number_raises_the_named_error(self) -> None:
        replies = (
            '{"found": true, "x": "abc", "y": 2, "confidence": 0.5}',
            '{"found": true, "x": 1e999, "y": 2, "confidence": 0.5}',
            '{"found": true, "x": NaN, "y": 2, "confidence": 0.5}',
            '{"found": true, "x": true, "y": 2, "confidence": 0.5}',
            '{"found": true, "y": 2, "confidence": 0.5}',
            '{"found": true, "x": 1, "y": null, "confidence": 0.5}',
            '{"found": true, "x": 1, "y": 2, "confidence": "high"}',
            '{"found": true, "x": 1, "y": 2, "confidence": true}',
            '{"found": true, "x": 1, "y": 2, "confidence": 1e999}',
            '{"found": "yes", "x": 1, "y": 2, "confidence": 0.5}',
            '{"found": true, "x": 1' + "0" * 400 + ', "y": 2, "confidence": 0.5}',
        )
        for reply in replies:
            with self.subTest(reply=reply[:40]), self.assertRaises(VisionReplyError):
                await self._find(reply)

    async def test_a_point_outside_the_screenshot_raises_the_named_error(self) -> None:
        width, height = SCREENSHOT_SIZE
        for x, y in ((width, 0), (0, height), (-1, 0), (0, -1), (width + 500, height + 500)):
            with self.subTest(x=x, y=y):
                with self.assertRaises(VisionReplyError) as raised:
                    await self._find(json.dumps({"found": True, "x": x, "y": y, "confidence": 0.9}))
                self.assertIn("outside the screenshot", str(raised.exception))

    async def test_the_bounds_come_from_the_screenshot_that_was_sent(self) -> None:
        small = self.tmp_path / "small.png"
        Image.new("RGB", (100, 60)).save(small)

        inside = await self._find(json.dumps({"found": True, "x": 99, "y": 59, "confidence": 0.9}), small)
        with self.assertRaises(VisionReplyError):
            await self._find(json.dumps({"found": True, "x": 100, "y": 59, "confidence": 0.9}), small)

        self.assertEqual((inside["x"], inside["y"]), (99, 59))

    async def test_a_found_reply_without_a_confidence_keeps_its_default(self) -> None:
        result = await self._find('{"found": true, "x": 3, "y": 4}')

        self.assertEqual((result["x"], result["y"], result["confidence"]), (3, 4, 0.0))

    async def test_a_not_found_reply_is_a_normal_answer_and_its_coordinates_are_not_read(self) -> None:
        for reply in (
            '{"found": false}',
            '{"found": false, "x": "junk", "y": null, "confidence": "high"}',
            '{"found": false, "x": 1e999, "y": true}',
        ):
            with self.subTest(reply=reply):
                result = await self._find(reply)
                self.assertFalse(result["found"])
                self.assertEqual((result["x"], result["y"]), (0, 0))

    async def test_a_garbled_reply_is_neither_returned_nor_logged(self) -> None:
        canary = "garbled-page-text-3f1a"
        reply = f"{canary} is what the page said"

        with self.assertLogs("app.vision_target", level="WARNING") as logs:
            with self.assertRaises(VisionReplyError) as raised:
                await self._find(reply)

        self.assertNotIn(canary, str(raised.exception))
        self.assertNotIn(canary, "\n".join(logs.output))
        self.assertIn(str(len(reply)), logs.output[0])
        self.assertIn("JSONDecodeError", logs.output[0])

    async def test_the_gateway_reports_a_garbled_reply_as_a_tool_error(self) -> None:
        shot = self._screenshot_path()
        manager = SimpleNamespace(
            ensure_session_accessible=AsyncMock(),
            get_session=AsyncMock(return_value=SimpleNamespace(id="s1")),
            capture_screenshot=AsyncMock(return_value={"screenshot_path": str(shot)}),
            settings=SimpleNamespace(force_governed_approvals=False),
        )
        gateway = McpToolGateway(
            manager=manager,
            orchestrator=SimpleNamespace(),
            job_queue=SimpleNamespace(),
            tool_profile="full",
            vision_targeter=_make_targeter(),
        )

        with patch("app.vision_target.httpx.AsyncClient", return_value=_mock_async_client(_reply("not json"))):
            response = await gateway.call_tool(
                McpToolCallRequest(name="browser.find_by_vision", arguments={"session_id": "s1", "description": "btn"})
            )

        self.assertTrue(response.isError)
        self.assertNotIn("found", response.structuredContent)
        self.assertIn("vision", response.content[0].text.lower())

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
