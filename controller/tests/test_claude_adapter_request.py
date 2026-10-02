"""The Claude adapter's request works on current Claude models.

It forced the tool (`tool_choice: {"type": "tool"}`), which Claude Opus 5.5,
Sonnet 5.5 and Fable 5.1 reject with a 400 — so CLAUDE_MODEL set to any of them
failed every agent step — and capped output at 1024 tokens while thinking, which
those models always do, counts against max_tokens.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image

from app.config import Settings
from app.providers.claude_adapter import ClaudeAdapter

TOOL_USE = {
    "type": "tool_use",
    "id": "toolu_1",
    "name": "browser_action",
    "input": {"action": "click", "reason": "open the menu", "element_id": "op-1"},
}


class ClaudeAdapterRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.screenshot = Path(self.tempdir.name) / "shot.png"
        Image.new("RGB", (8, 8), "white").save(self.screenshot)
        self.adapter = ClaudeAdapter(
            Settings(_env_file=None, ANTHROPIC_API_KEY="test-key", CLAUDE_MODEL="claude-opus-5-5")
        )

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def _decide(self, response: dict):
        self.adapter._post_json = AsyncMock(return_value=response)  # type: ignore[method-assign]
        decision = await self.adapter._decide(
            goal="Open the menu",
            observation={"screenshot_path": str(self.screenshot), "url": "https://example.com", "title": "Example"},
            context_hints=None,
            previous_steps=[],
            model_override=None,
        )
        return decision, self.adapter._post_json.await_args.kwargs["payload"]

    async def test_the_tool_is_requested_not_forced(self) -> None:
        decision, payload = await self._decide(
            {"content": [TOOL_USE], "model": "claude-opus-5-5", "stop_reason": "tool_use"}
        )

        self.assertEqual(payload["tool_choice"], {"type": "auto", "disable_parallel_tool_use": True})
        self.assertIn("browser_action", payload["system"])
        self.assertGreaterEqual(payload["max_tokens"], 16000)
        self.assertEqual(decision.decision.element_id, "op-1")

    async def test_a_turn_without_the_tool_says_why(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            await self._decide({"content": [], "model": "claude-opus-5-5", "stop_reason": "refusal"})
        self.assertIn("refusal", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
