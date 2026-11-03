"""The controller's copy of the stdio bridge (app.mcp_stdio, the Glama entrypoint) speaks UTF-8 whatever the console's code page.

MCP's stdio transport is UTF-8. A client that launches the bridge with pipes on Windows hands it the
locale's code page (cp1252) for stdin and stdout, so tools/list went out as invalid UTF-8 and a page with
text outside cp1252 crashed the bridge mid-session. PYTHONIOENCODING=cp1252 recreates that on any OS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONTROLLER_ROOT = Path(__file__).resolve().parents[1]
# Outside cp1252: an em dash is in it (0x97), CJK, Arabic and Cyrillic are not.
PAGE_TEXT = "Example Domain — 例のドメイン — مثال — Пример"


class _FakeController(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        self.server.received.append(request)  # type: ignore[attr-defined]
        # A lone surrogate, as a page's broken text can carry: JSON escapes it, UTF-8 cannot encode it.
        result = {"text": PAGE_TEXT, "broken": "\udc80", "echo": request.get("params")}
        body = json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("MCP-Session-Id", "s-1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_: object) -> None:
        return


def _run_bridge(lines: list[dict]) -> tuple[bytes, bytes, list[dict]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeController)
    server.received = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONUTF8", "PYTHONIOENCODING"}}
    env.update({"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0", "PYTHONPATH": str(CONTROLLER_ROOT)})
    env.pop("AUTO_BROWSER_BEARER_TOKEN", None)
    env.pop("AUTO_BROWSER_TOKEN_FILE", None)
    try:
        stdin = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines).encode("utf-8")
        completed = subprocess.run(
            [sys.executable, "-m", "app.mcp_stdio", "--base-url", f"http://127.0.0.1:{server.server_port}/mcp"],
            input=stdin,
            capture_output=True,
            env=env,
            timeout=60,
        )
    finally:
        server.shutdown()
    return completed.stdout, completed.stderr, server.received  # type: ignore[attr-defined]


def test_the_bridge_writes_utf8_and_reads_utf8_under_a_cp1252_console() -> None:
    argument = "naïve — 日本語"
    stdout, stderr, received = _run_bridge(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "x", "arguments": {"q": argument}}},
        ]
    )

    assert b"Traceback" not in stderr, stderr.decode("utf-8", "replace")
    responses = [json.loads(line) for line in stdout.decode("utf-8").splitlines()]
    assert [r["id"] for r in responses] == [1, 2]
    assert all(r["result"]["text"] == PAGE_TEXT for r in responses)
    # The lone surrogate goes out as its JSON escape, which decodes back to it.
    assert responses[1]["result"]["broken"] == "\udc80"
    # The client's UTF-8 argument reached the controller intact.
    assert received[1]["params"]["arguments"]["q"] == argument
