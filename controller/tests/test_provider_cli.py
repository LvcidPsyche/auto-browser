from __future__ import annotations

import json
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.providers.base import CLIResult, ProviderAPIError
from app.providers.claude_adapter import ClaudeAdapter
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.openai_adapter import OpenAIAdapter


class HealthzUnixSocketServer:
    class _Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.recv(4096)
            self.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 15\r\n"
                b"Connection: close\r\n\r\n"
                b"{\"status\":\"ok\"}"
            )

    def __init__(self, socket_path: Path) -> None:
        class _Server(socketserver.UnixStreamServer):
            allow_reuse_address = True

        self.socket_path = socket_path
        self.server = _Server(str(socket_path), self._Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "HealthzUnixSocketServer":
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.socket_path.exists():
            self.socket_path.unlink()


class FakeAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, json=None):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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
            self.assertIn("-c", command)
            self.assertIn("agents={}", command)
            self.assertIn("mcp_servers={}", command)
            self.assertIn("--output-schema", command)
            self.assertIn("--output-last-message", command)
            self.assertIn("--image", command)
            schema_path = Path(command[command.index("--output-schema") + 1])
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            self.assertEqual(schema["required"], list(schema["properties"].keys()))
            self.assertNotIn("default", json.dumps(schema))
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

    async def test_openai_host_bridge_mode_parses_response(self) -> None:
        settings = Settings(
            _env_file=None,
            OPENAI_AUTH_MODE="host_bridge",
            OPENAI_HOST_BRIDGE_SOCKET=str(Path(self.tempdir.name) / "codex.sock"),
            OPENAI_CLI_MODEL="gpt-5.4",
        )
        Path(settings.openai_host_bridge_socket).write_text("", encoding="utf-8")
        adapter = OpenAIAdapter(settings)

        with patch.object(
            adapter,
            "_post_host_bridge_request",
            new=AsyncMock(
                return_value={
                    "model": "gpt-5.4",
                    "raw_text": '{"action":"done","reason":"complete","risk_category":"read"}',
                }
            ),
        ):
            result = await adapter._decide(
                goal="Finish the task",
                observation=self.observation,
                context_hints=None,
                previous_steps=[],
                model_override=None,
            )

        self.assertEqual(result.model, "gpt-5.4")
        self.assertEqual(result.decision.action, "done")
        self.assertEqual(result.usage, {"auth_mode": "host_bridge", "transport": "codex-host-bridge"})

    async def test_openai_host_bridge_mode_sends_strict_schema(self) -> None:
        settings = Settings(
            _env_file=None,
            OPENAI_AUTH_MODE="host_bridge",
            OPENAI_HOST_BRIDGE_SOCKET=str(Path(self.tempdir.name) / "codex.sock"),
            OPENAI_CLI_MODEL="gpt-5.4",
        )
        Path(settings.openai_host_bridge_socket).write_text("", encoding="utf-8")
        adapter = OpenAIAdapter(settings)

        captured: dict[str, object] = {}

        async def fake_post(payload: dict[str, object]) -> dict[str, object]:
            captured.update(payload)
            return {
                "model": "gpt-5.4",
                "raw_text": '{"action":"done","reason":"complete","risk_category":"read"}',
            }

        with patch.object(adapter, "_post_host_bridge_request", new=AsyncMock(side_effect=fake_post)):
            result = await adapter._decide(
                goal="Finish the task",
                observation=self.observation,
                context_hints=None,
                previous_steps=[],
                model_override=None,
            )

        schema = captured["schema"]
        self.assertIsInstance(schema, dict)
        self.assertEqual(schema["required"], list(schema["properties"].keys()))
        self.assertNotIn("default", json.dumps(schema))
        self.assertEqual(result.model, "gpt-5.4")
        self.assertEqual(result.decision.action, "done")
        self.assertEqual(result.usage, {"auth_mode": "host_bridge", "transport": "codex-host-bridge"})

    async def test_openai_host_bridge_request_retries_rate_limits(self) -> None:
        settings = Settings(
            _env_file=None,
            OPENAI_AUTH_MODE="host_bridge",
            OPENAI_HOST_BRIDGE_SOCKET=str(Path(self.tempdir.name) / "codex.sock"),
        )
        settings.model_max_retries = 1
        settings.model_retry_backoff_seconds = 0
        adapter = OpenAIAdapter(settings)

        request = httpx.Request("POST", "http://host-bridge/openai/decide")
        responses = [
            httpx.Response(429, request=request, json={"error": {"message": "rate limited"}}),
            httpx.Response(200, request=request, json={"raw_text": "{}", "model": "codex-default"}),
        ]

        with patch("app.providers.openai_adapter.httpx.AsyncClient", return_value=FakeAsyncClient(responses)):
            payload = await adapter._post_host_bridge_request({"demo": True})

        self.assertEqual(payload["model"], "codex-default")

    async def test_openai_host_bridge_request_marks_final_rate_limit_retryable(self) -> None:
        settings = Settings(
            _env_file=None,
            OPENAI_AUTH_MODE="host_bridge",
            OPENAI_HOST_BRIDGE_SOCKET=str(Path(self.tempdir.name) / "codex.sock"),
        )
        settings.model_max_retries = 1
        settings.model_retry_backoff_seconds = 0
        adapter = OpenAIAdapter(settings)

        request = httpx.Request("POST", "http://host-bridge/openai/decide")
        responses = [
            httpx.Response(429, request=request, json={"error": {"message": "rate limited"}}),
            httpx.Response(429, request=request, json={"error": {"message": "still limited"}}),
        ]

        with patch("app.providers.openai_adapter.httpx.AsyncClient", return_value=FakeAsyncClient(responses)):
            with self.assertRaises(ProviderAPIError) as ctx:
                await adapter._post_host_bridge_request({"demo": True})

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("still limited", str(ctx.exception))

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
        self.assertIn("api, cli, host_bridge", adapter.readiness_detail)

    def test_host_bridge_mode_checks_socket_path(self) -> None:
        socket_path = Path(self.tempdir.name) / "codex.sock"
        adapter = OpenAIAdapter(
            Settings(
                _env_file=None,
                OPENAI_AUTH_MODE="host_bridge",
                OPENAI_HOST_BRIDGE_SOCKET=str(socket_path),
            )
        )

        self.assertFalse(adapter.configured)
        self.assertIn("does not exist", adapter.readiness_detail)

        socket_path.write_text("", encoding="utf-8")
        self.assertFalse(adapter.configured)
        self.assertIn("is not a Unix socket", adapter.readiness_detail)

        socket_path.unlink()
        with HealthzUnixSocketServer(socket_path):
            self.assertTrue(adapter.configured)
            self.assertIn("ready via OpenAI host bridge socket", adapter.readiness_detail)


if __name__ == "__main__":
    unittest.main()
