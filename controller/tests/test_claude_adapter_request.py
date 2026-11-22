"""The Claude adapter's request works on current Claude models.

It forced the tool (`tool_choice: {"type": "tool"}`), which Claude Opus 5.5,
Sonnet 5.5 and Fable 5.1 reject with a 400 — so CLAUDE_MODEL set to any of them
failed every agent step — and capped output at 1024 tokens while thinking, which
those models always do, counts against max_tokens.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image

from app.config import Settings
from app.providers.base import PAGE_DATA_NOTICE, CLIResult
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

    async def test_the_system_prompt_says_the_observations_page_strings_are_data(self) -> None:
        # NAV-12: page text, titles and element names reach the planner, and nothing framed them as the page's.
        _, payload = await self._decide({"content": [TOOL_USE], "model": "claude-opus-5-5", "stop_reason": "tool_use"})

        self.assertIn(PAGE_DATA_NOTICE, payload["system"])

    async def test_a_turn_without_the_tool_says_why(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            await self._decide({"content": [], "model": "claude-opus-5-5", "stop_reason": "refusal"})
        self.assertIn("refusal", str(ctx.exception))


class ClaudeCliCommandTests(unittest.IsolatedAsyncioTestCase):
    """Claude Code loads no MCP server: --strict-mcp-config, before the prompt, leaves it only the servers a
    --mcp-config names, and the command has none (a bare --mcp-config takes every word after it, the prompt too)."""

    async def _cli_command(self, model: str) -> list[str]:
        """The command one CLI decision runs, with CLAUDE_CLI_MODEL ``model``."""
        adapter = ClaudeAdapter(Settings(_env_file=None, CLAUDE_AUTH_MODE="cli", CLAUDE_CLI_MODEL=model))
        stdout = '{"result": {"action": "done", "reason": "complete", "risk_category": "read"}}'
        adapter.run_cli = AsyncMock(  # type: ignore[method-assign]
            return_value=CLIResult(command=["claude"], stdout=stdout, stderr="", returncode=0)
        )
        await adapter._decide(
            goal="Open the menu", observation={"url": "https://example.com"}, context_hints=None,
            previous_steps=[], model_override=None,
        )  # fmt: skip
        return adapter.run_cli.await_args.kwargs["command"]

    async def test_the_cli_is_told_to_load_no_mcp_server(self) -> None:
        for model in ("", "sonnet"):
            with self.subTest(model=model):
                command = await self._cli_command(model)

                self.assertIn("--strict-mcp-config", command)
                self.assertEqual([word for word in command if word.startswith("--mcp-config")], [])
                self.assertIn("Choose exactly one next browser action", command[-1], "the prompt is the last word")

    async def test_the_cli_runs_no_hook_and_reads_no_instruction_file(self) -> None:
        # One --settings value, merged over the user's settings files: no hook, no CLAUDE.md, CLAUDE.local.md, rules
        # or auto memory file, no LSP tool; nothing of the sign-in, the endpoint or the model.
        for model in ("", "sonnet"):
            with self.subTest(model=model):
                command = await self._cli_command(model)

                self.assertEqual(command.count("--settings"), 1)
                self.assertNotIn("--setting-sources", command)
                value = command.index("--settings") + 1
                self.assertEqual(
                    json.loads(command[value]),
                    {
                        "disableAllHooks": True,
                        "claudeMdExcludes": ["**"],
                        "autoMemoryEnabled": False,
                        "permissions": {"deny": ["LSP"]},
                    },
                )


if __name__ == "__main__":
    unittest.main()
