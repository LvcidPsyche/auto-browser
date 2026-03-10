from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.providers.base import CLIResult
from app.providers.claude_adapter import ClaudeAdapter
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.openai_adapter import OpenAIAdapter


class ProviderCLITests(unittest.IsolatedAsyncioTestCase):
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

    def touch(self, relative_path: str, content: str = "ok") -> None:
        path = Path(self.tempdir.name) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    async def test_openai_cli_mode_reads_structured_output_file(self) -> None:
        settings = Settings(
            _env_file=None,
            OPENAI_AUTH_MODE="cli",
            OPENAI_CLI_PATH="codex",
            OPENAI_CLI_MODEL="gpt-5-codex",
        )
        adapter = OpenAIAdapter(settings)

        async def fake_run_cli(*, command, input_text=None, env=None, cwd=None):
            self.assertIn("exec", command)
            self.assertIn("--output-schema", command)
            self.assertIn("--output-last-message", command)
            self.assertIn("--image", command)
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(
                '{"action":"done","reason":"complete","risk_category":"read"}',
                encoding="utf-8",
            )
            self.assertIn("Choose exactly one next browser action", input_text or "")
            return CLIResult(command=command, stdout="", stderr="", returncode=0)

        with patch.object(adapter, "run_cli", new=AsyncMock(side_effect=fake_run_cli)):
            result = await adapter._decide(
                goal="Finish the task",
                observation=self.observation,
                context_hints=None,
                previous_steps=[],
                model_override=None,
            )

        self.assertEqual(result.model, "gpt-5-codex")
        self.assertEqual(result.decision.action, "done")
        self.assertEqual(result.usage, {"auth_mode": "cli", "transport": "codex-exec"})

    async def test_claude_cli_mode_parses_nested_json_output(self) -> None:
        settings = Settings(
            _env_file=None,
            CLAUDE_AUTH_MODE="cli",
            CLAUDE_CLI_PATH="claude",
            CLAUDE_CLI_MODEL="sonnet",
        )
        adapter = ClaudeAdapter(settings)

        fake_stdout = '{"result":{"action":"done","reason":"complete","risk_category":"read"}}'
        with patch.object(
            adapter,
            "run_cli",
            new=AsyncMock(return_value=CLIResult(command=["claude"], stdout=fake_stdout, stderr="", returncode=0)),
        ):
            result = await adapter._decide(
                goal="Finish the task",
                observation=self.observation,
                context_hints=None,
                previous_steps=[],
                model_override=None,
            )

        self.assertEqual(result.model, "sonnet")
        self.assertEqual(result.decision.action, "done")
        self.assertEqual(result.decision.risk_category, "read")

    async def test_gemini_cli_mode_parses_json_embedded_in_text(self) -> None:
        settings = Settings(
            _env_file=None,
            GEMINI_AUTH_MODE="cli",
            GEMINI_CLI_PATH="gemini",
            GEMINI_CLI_MODEL="gemini-2.5-pro",
        )
        adapter = GeminiAdapter(settings)

        fake_stdout = 'decision follows\n{"decision":{"action":"done","reason":"complete","risk_category":"read"}}'
        with patch.object(
            adapter,
            "run_cli",
            new=AsyncMock(return_value=CLIResult(command=["gemini"], stdout=fake_stdout, stderr="", returncode=0)),
        ):
            result = await adapter._decide(
                goal="Finish the task",
                observation=self.observation,
                context_hints=None,
                previous_steps=[],
                model_override=None,
            )

        self.assertEqual(result.model, "gemini-2.5-pro")
        self.assertEqual(result.decision.action, "done")

    def test_cli_configured_checks_binary_path(self) -> None:
        self.touch(".codex/auth.json")
        self.touch(".claude.json")
        self.touch(".gemini/config.json")
        with patch("app.providers.base.which", return_value="/usr/bin/fake"):
            self.assertTrue(
                OpenAIAdapter(
                    Settings(_env_file=None, OPENAI_AUTH_MODE="cli", CLI_HOME=self.tempdir.name)
                ).configured
            )
            self.assertTrue(
                ClaudeAdapter(
                    Settings(_env_file=None, CLAUDE_AUTH_MODE="cli", CLI_HOME=self.tempdir.name)
                ).configured
            )
            self.assertTrue(
                GeminiAdapter(
                    Settings(_env_file=None, GEMINI_AUTH_MODE="cli", CLI_HOME=self.tempdir.name)
                ).configured
            )

    def test_cli_configured_requires_auth_state_when_cli_home_is_set(self) -> None:
        settings = Settings(
            _env_file=None,
            OPENAI_AUTH_MODE="cli",
            OPENAI_CLI_PATH="codex",
            CLI_HOME=self.tempdir.name,
        )
        adapter = OpenAIAdapter(settings)

        with patch("app.providers.base.which", return_value="/usr/bin/codex"):
            self.assertFalse(adapter.configured)

        self.assertIn("No openai CLI auth state found", adapter.readiness_detail)

    def test_invalid_auth_mode_is_reported(self) -> None:
        adapter = OpenAIAdapter(
            Settings(
                _env_file=None,
                OPENAI_AUTH_MODE="bogus",
                OPENAI_API_KEY="test-key",
            )
        )

        self.assertFalse(adapter.configured)
        self.assertIn("invalid", adapter.readiness_detail)
        self.assertIn("api, cli", adapter.readiness_detail)


if __name__ == "__main__":
    unittest.main()
