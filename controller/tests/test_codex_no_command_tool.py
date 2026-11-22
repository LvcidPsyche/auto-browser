"""A Codex decision call has no command tool, with the real codex binary: the controller's adapter (CLI mode) and the
host bridge each make one decision against a stand-in Responses API model that asks the shell tool Codex offers to
run a command. Neither call may offer one, the command's output never reaches the model, and the decision comes back.

Skipped where codex is not installed (the controller image built without INSTALL_AGENT_CLIS=true, most hosts).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import Settings
from app.providers.openai_adapter import OpenAIAdapter
from tests._codex_bridge import BRIDGE_SCRIPT, load_bridge

CODEX = shutil.which("codex")
SHELL_TOOLS = ("exec_command", "shell_command", "shell", "write_stdin")
DECISION = '{"action":"done","reason":"stand-in","risk_category":"read"}'

pytestmark = pytest.mark.skipif(CODEX is None or os.name == "nt", reason="needs the codex binary (and POSIX)")


class _Model:
    """A stand-in Responses API model on loopback: it records the tools of each request and what came back to it,
    asks the first shell tool offered to run `echo <token>`, then answers DECISION."""

    def __init__(self) -> None:
        self.token = "ran-" + secrets.token_hex(8)
        self.requests: list[dict] = []
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _send(self, content_type: str, body: bytes) -> None:
                self.send_response(200)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._send("application/json", json.dumps({"models": [], "data": []}).encode())

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                self._send("text/event-stream", model.answer(json.loads(raw or b"{}")))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v1" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, request: dict) -> bytes:
        tools = [tool.get("name") or tool.get("type") for tool in request.get("tools", []) or []]
        outputs = [item for item in request.get("input", []) if item.get("type") == "function_call_output"]
        answered = bool(outputs)
        came_back = any(self.token in json.dumps(item.get("output")) for item in outputs)
        self.requests.append({"tools": tools, "token_came_back": came_back})
        shell = next((name for name in SHELL_TOOLS if name in tools and name != "write_stdin"), None)
        if shell and not answered:
            command = "echo " + self.token
            arguments = {"exec_command": {"cmd": command}, "shell_command": {"command": command}}.get(
                shell, {"command": ["sh", "-c", command]}
            )
            item = {"type": "function_call", "name": shell, "arguments": json.dumps(arguments), "call_id": "c1"}
        else:
            content = [{"type": "output_text", "text": DECISION}]
            item = {"type": "message", "role": "assistant", "id": "m1", "content": content}
        usage = {"input_tokens": 0, "input_tokens_details": None, "output_tokens": 0, "output_tokens_details": None}
        events = (
            {"type": "response.created", "response": {"id": "r1"}},
            {"type": "response.output_item.done", "item": item},
            {"type": "response.completed", "response": {"id": "r1", "usage": {**usage, "total_tokens": 0}}},
        )
        return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def model():
    stand_in = _Model()
    yield stand_in
    stand_in.close()


def _codex_home(home: Path, model: _Model) -> None:
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "config.toml").write_text(
        'model = "mock-model"\nmodel_provider = "mock"\n[model_providers.mock]\nname = "mock"\n'
        f'base_url = "{model.url}"\nwire_api = "responses"\n',
        encoding="utf-8",
    )
    (home / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "placeholder"}\n', encoding="utf-8")


def _assert_no_command_ran(model: _Model) -> None:
    assert model.requests, "the model was never asked"
    offered = sorted({name for request in model.requests for name in request["tools"] if name in SHELL_TOOLS})
    assert not any(request["token_came_back"] for request in model.requests), f"the command ran ({offered})"
    assert offered == [], f"Codex offered the model {offered}"


def test_the_adapters_codex_call_has_no_command_tool(model, tmp_path: Path) -> None:
    home = tmp_path / "cli-home"
    _codex_home(home, model)
    settings = Settings(_env_file=None, OPENAI_AUTH_MODE="cli", OPENAI_CLI_PATH=CODEX, CLI_HOME=str(home))
    with patch.dict(os.environ, {"OPENAI_API_KEY": "placeholder"}):
        os.environ.pop("CODEX_HOME", None)
        result = asyncio.run(
            OpenAIAdapter(settings)._decide(
                goal="g",
                observation={"url": "https://example.com"},
                context_hints=None,
                previous_steps=[],
                model_override=None,
            )  # fmt: skip
        )

    _assert_no_command_ran(model)
    assert result.decision.action == "done"


@pytest.mark.skipif(not BRIDGE_SCRIPT.is_file(), reason="scripts/ is not shipped in the controller image")
def test_the_host_bridges_codex_call_has_no_command_tool(model, tmp_path: Path) -> None:
    bridge = load_bridge("codex_host_bridge_no_command_under_test")
    home = tmp_path / "bridge-home"
    _codex_home(home, model)
    schema = {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}
    with patch.dict(os.environ, {"HOME": str(home), "OPENAI_API_KEY": "placeholder"}):
        os.environ.pop("CODEX_HOME", None)
        reply = bridge.CodexBridgeService(codex_path=CODEX, request_timeout_seconds=120.0).handle_openai_decide(
            {"prompt": "decide the next browser action", "schema": schema}
        )

    _assert_no_command_ran(model)
    assert json.loads(reply["raw_text"])["action"] == "done"
