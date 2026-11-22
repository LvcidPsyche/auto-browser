"""A Gemini decision call has no tool, with the real gemini binary: the controller's adapter (CLI mode) makes one
decision against a stand-in Gemini API that asks for read_file of a credential file planted in the CLI home, where the
CLI declares read_file, and asks for it anyway where it does not. No tool may be declared, the file's marker never
reaches the model, and the decision comes back. The stand-in is reached through GOOGLE_GEMINI_BASE_URL, which only
this test passes (CLI_PROVIDER_ENV_PASSTHROUGH).

Skipped where gemini is not installed (the controller image built without INSTALL_AGENT_CLIS=true, most hosts).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import Settings
from app.providers.gemini_adapter import GeminiAdapter

GEMINI = shutil.which("gemini")
MARKER = "credential-marker-7d2f"
DECISION = '{"action": "done", "reason": "stand-in", "risk_category": "read"}'

pytestmark = pytest.mark.skipif(GEMINI is None or os.name == "nt", reason="needs the gemini binary (and POSIX)")


class _Model:
    """A stand-in Gemini API on loopback: it records the declarations of each request and whether the marker was in
    it; on the first streamed turn it asks for read_file of ``path`` (where declared, or, ``forced``, anyway), then
    answers DECISION."""

    def __init__(self, path: Path, *, forced: bool) -> None:
        self.requests: list[dict] = []
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _send(self, content_type: str, body: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._send("application/json", b'{"models": []}')

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                request = json.loads(raw or b"{}")
                declared = [
                    declaration.get("name")
                    for tool in request.get("tools") or []
                    if isinstance(tool, dict)
                    for declaration in tool.get("functionDeclarations") or []
                ]
                answered = any(
                    "functionResponse" in part
                    for content in request.get("contents") or []
                    for part in content.get("parts") or []
                )
                model.requests.append({"declared": declared, "marker": MARKER.encode() in raw})
                route = self.path.split("?")[0]
                if route.endswith(":countTokens"):
                    body: dict = {"totalTokens": 1}
                elif "streamGenerateContent" in route and not answered and (forced or "read_file" in declared):
                    arguments = {"absolute_path": str(path), "file_path": str(path), "path": str(path)}
                    body = _candidate([{"functionCall": {"name": "read_file", "args": arguments}}])
                else:
                    body = _candidate([{"text": DECISION}])
                if "streamGenerateContent" in route:
                    self._send("text/event-stream", b"data: " + json.dumps(body).encode() + b"\n\n")
                else:
                    self._send("application/json", json.dumps(body).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _candidate(parts: list[dict]) -> dict:
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
    }


@pytest.mark.parametrize("forced", [False, True], ids=["read_file where declared", "read_file asked for anyway"])
def test_the_adapters_gemini_call_declares_no_tool(forced: bool, tmp_path: Path) -> None:
    home = tmp_path / "cli-home"
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "settings.json").write_text(
        json.dumps({"security": {"auth": {"selectedType": "gemini-api-key"}}}), encoding="utf-8"
    )
    credential = home / ".gemini" / "oauth_creds.json"
    credential.write_text(json.dumps({"access_token": MARKER, "refresh_token": MARKER}), encoding="utf-8")
    model = _Model(credential, forced=forced)
    settings = Settings(
        _env_file=None,
        GEMINI_AUTH_MODE="cli",
        GEMINI_CLI_PATH=GEMINI,
        CLI_HOME=str(home),
        CLI_PROVIDER_ENV_PASSTHROUGH="GOOGLE_GEMINI_BASE_URL",
    )
    adapter = GeminiAdapter(settings)
    printed: list[str] = []
    run_cli = adapter.run_cli

    async def recording_run_cli(**kwargs):
        result = await run_cli(**kwargs)
        printed.append(result.stdout)
        return result

    try:
        with (
            patch.object(adapter, "run_cli", side_effect=recording_run_cli),
            patch.dict(os.environ, {"GEMINI_API_KEY": "placeholder", "GOOGLE_GEMINI_BASE_URL": model.url}),
            contextlib.suppress(RuntimeError),  # what the adapter makes of the reply is not what this test is about
        ):
            asyncio.run(
                adapter._decide(
                    goal="g",
                    observation={"url": "https://example.com"},
                    context_hints=None,
                    previous_steps=[],
                    model_override=None,
                )  # fmt: skip
            )
    finally:
        model.close()

    assert model.requests, "the model was never asked"
    assert not any(request["marker"] for request in model.requests), "the credential file reached the model"
    assert [request["declared"] for request in model.requests if request["declared"]] == []
    # The decision came back: the CLI's reply (--output-format json) holds it as the model's response.
    (reply,) = printed
    assert json.loads(json.loads(reply)["response"])["action"] == "done"
