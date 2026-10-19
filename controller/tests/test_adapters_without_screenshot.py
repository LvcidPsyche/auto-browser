"""Every provider adapter handles an observation that has no screenshot.

observe(preset="text") returns `screenshot_path: None`, and PERCEPTION_PRESET_DEFAULT=text
makes that the default. The adapters did `encode_image(observation["screenshot_path"])`, so
every vision adapter died with KeyError (key absent) or TypeError (key None, the real shape)
before it sent anything. With no screenshot they now send a text-only request.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.providers.base import CLIResult
from app.providers.claude_adapter import ClaudeAdapter
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.openai_adapter import OpenAIAdapter
from app.providers.openai_compatible import OPENAI_COMPATIBLE_PROFILES, OpenAICompatibleAdapter

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Smallest valid PNG, so encode_image() has a real file to read.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001a5f645400000000049454e44ae426082"
)
_PNG_B64 = base64.b64encode(_PNG).decode("ascii")

_BASE_OBSERVATION: dict[str, Any] = {
    "url": "https://example.com",
    "title": "Example",
    "text_excerpt": "hello",
    "interactables": [],
    "preset": "text",
}
_ENV_KEYS = {
    "ANTHROPIC_API_KEY": "k",
    "GEMINI_API_KEY": "k",
    "OPENAI_API_KEY": "k",
    "OPENROUTER_API_KEY": "k",
    "XAI_API_KEY": "k",
    "DEEPSEEK_API_KEY": "k",
    "MINIMAX_API_KEY": "k",
    "OPENAI_COMPATIBLE_API_KEY": "k",
}


def _settings(**extra: Any) -> Settings:
    return Settings(_env_file=None, **_ENV_KEYS, **extra)


def _api_adapters() -> list[tuple[str, Any, bool]]:
    """(name, adapter, can_send_images) for every adapter that has an API request."""
    settings = _settings()
    adapters: list[tuple[str, Any, bool]] = [
        ("claude", ClaudeAdapter(settings), True),
        ("gemini", GeminiAdapter(settings), True),
        ("openai", OpenAIAdapter(settings), True),
    ]
    for profile in OPENAI_COMPATIBLE_PROFILES:
        adapters.append(
            (f"compatible[{profile.provider}]", OpenAICompatibleAdapter(settings, profile), profile.supports_vision)
        )
    return adapters


class AdaptersWithoutScreenshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.screenshot = Path(self.tempdir.name) / "shot.png"
        self.screenshot.write_bytes(_PNG)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def _request_payload(self, adapter: Any, observation: dict[str, Any]) -> dict[str, Any]:
        adapter._post_json = AsyncMock(return_value={})
        try:
            await adapter._decide(
                goal="g",
                observation=observation,
                context_hints=None,
                previous_steps=[],
                model_override=None,
            )
        except RuntimeError:
            # The empty reply has no decision in it; only the request matters here.
            pass
        adapter._post_json.assert_awaited_once()
        return adapter._post_json.await_args.kwargs["payload"]

    async def test_no_screenshot_sends_a_text_only_request(self) -> None:
        variants = {
            "key absent": dict(_BASE_OBSERVATION),
            "key None": {**_BASE_OBSERVATION, "screenshot_path": None, "screenshot_url": None},
        }
        for name, adapter, _can_send_images in _api_adapters():
            for variant, observation in variants.items():
                with self.subTest(adapter=name, observation=variant):
                    body = json.dumps(await self._request_payload(adapter, observation))
                    self.assertNotIn("base64", body)
                    for image_marker in ('"image"', "inlineData", "input_image", "image_url"):
                        self.assertNotIn(image_marker, body)
                    self.assertIn("Choose exactly one next browser action", body)

    async def test_a_real_screenshot_is_still_sent(self) -> None:
        observation = {**_BASE_OBSERVATION, "screenshot_path": str(self.screenshot)}
        for name, adapter, can_send_images in _api_adapters():
            with self.subTest(adapter=name):
                body = json.dumps(await self._request_payload(adapter, observation))
                if can_send_images:
                    self.assertIn(_PNG_B64, body)
                else:
                    # A text-only model profile never gets the image, with or without one.
                    self.assertNotIn(_PNG_B64, body)

    async def test_the_openai_codex_cli_omits_image_without_a_screenshot(self) -> None:
        adapter = OpenAIAdapter(_settings(OPENAI_AUTH_MODE="cli", OPENAI_CLI_PATH="codex"))
        commands: list[list[str]] = []

        async def fake_run_cli(*, command, input_text=None, env=None, cwd=None):
            commands.append(command)
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text('{"action":"done","reason":"x","risk_category":"read"}', encoding="utf-8")
            return CLIResult(command=command, stdout="", stderr="", returncode=0)

        variants = {
            "key absent": (dict(_BASE_OBSERVATION), False),
            "key None": ({**_BASE_OBSERVATION, "screenshot_path": None}, False),
            "real screenshot": ({**_BASE_OBSERVATION, "screenshot_path": str(self.screenshot)}, True),
        }
        for variant, (observation, expect_image) in variants.items():
            with self.subTest(observation=variant):
                with patch.object(adapter, "run_cli", new=AsyncMock(side_effect=fake_run_cli)):
                    await adapter._decide(
                        goal="g", observation=observation, context_hints=None, previous_steps=[], model_override=None
                    )
                self.assertEqual("--image" in commands[-1], expect_image)

    async def test_the_openai_host_bridge_request_omits_screenshot_without_one(self) -> None:
        adapter = OpenAIAdapter(_settings(OPENAI_AUTH_MODE="host_bridge"))
        variants = {
            "key absent": (dict(_BASE_OBSERVATION), False),
            "key None": ({**_BASE_OBSERVATION, "screenshot_path": None}, False),
            "real screenshot": ({**_BASE_OBSERVATION, "screenshot_path": str(self.screenshot)}, True),
        }
        for variant, (observation, expect_image) in variants.items():
            with self.subTest(observation=variant):
                adapter._post_host_bridge_request = AsyncMock(return_value={"raw_text": "{}", "model": "m"})
                try:
                    await adapter._decide(
                        goal="g", observation=observation, context_hints=None, previous_steps=[], model_override=None
                    )
                except Exception:  # noqa: BLE001 - "{}" is not a decision; only the bridge request matters
                    pass
                request = adapter._post_host_bridge_request.await_args.args[0]
                self.assertEqual("screenshot" in request, expect_image)
                if expect_image:
                    self.assertEqual(request["screenshot"]["base64"], _PNG_B64)


class HostBridgeWithoutScreenshotTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import codex_host_bridge
        except Exception as exc:  # pragma: no cover - platform dependent
            raise unittest.SkipTest(f"codex_host_bridge unavailable: {exc}") from exc
        self.module = codex_host_bridge

    def _run(self, payload: dict[str, Any]) -> list[str]:
        """Run handle_openai_decide with codex mocked; return the command it built."""
        service = self.module.CodexBridgeService(codex_path="codex")
        seen: list[list[str]] = []

        def fake_run(command, **kwargs):
            seen.append(command)
            Path(command[command.index("--output-last-message") + 1]).write_text("{}", encoding="utf-8")
            return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

        with patch.object(self.module.subprocess, "run", side_effect=fake_run):
            service.handle_openai_decide(payload)
        return seen[0]

    def test_a_request_without_a_screenshot_runs_codex_without_an_image(self) -> None:
        command = self._run({"prompt": "p", "schema": {"type": "object"}})
        self.assertNotIn("--image", command)

    def test_a_request_with_a_screenshot_still_passes_the_image(self) -> None:
        command = self._run(
            {"prompt": "p", "schema": {"type": "object"}, "screenshot": {"media_type": "image/png", "base64": _PNG_B64}}
        )
        self.assertIn("--image", command)

    def test_a_malformed_screenshot_is_still_rejected(self) -> None:
        service = self.module.CodexBridgeService(codex_path="codex")
        with self.assertRaises(self.module.BridgeError):
            service.handle_openai_decide({"prompt": "p", "schema": {"type": "object"}, "screenshot": "nope"})


if __name__ == "__main__":
    unittest.main()
