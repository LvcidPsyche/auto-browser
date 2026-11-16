"""A stand-in for the model behind Codex's Responses API, for codex_sandbox_check.sh.

Its first answer asks Codex to run one shell command (the probe given on the command line) with whichever shell tool
Codex offers; its second, which carries the command's output, ends the turn. That output is appended to the log, so
the check reads what a command of the model's could do under each sandbox setting. Loopback only; no network needed.

usage: codex_responses_mock.py PORT LOG PROBE
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
LOG = sys.argv[2]
PROBE = sys.argv[3]
SHELL_TOOLS = ("shell", "shell_command", "exec_command")


def _events(*events: dict) -> bytes:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


def _completed(response_id: str) -> dict:
    usage = {"input_tokens": 0, "input_tokens_details": None, "output_tokens": 0, "output_tokens_details": None}
    return {"type": "response.completed", "response": {"id": response_id, "usage": {**usage, "total_tokens": 0}}}


def _shell_call(tools: list[dict]) -> tuple[str, dict]:
    """The shell tool Codex offers, with the probe in the argument shape that tool takes."""
    offered = {tool.get("name"): tool for tool in tools if tool.get("type") == "function"}
    for name in SHELL_TOOLS:
        if name in offered:
            command = offered[name].get("parameters", {}).get("properties", {}).get("command")
            if command is None:
                return name, {"cmd": PROBE}
            return name, {"command": ["sh", "-c", PROBE] if command.get("type") == "array" else PROBE}
    raise RuntimeError(f"Codex offered no shell tool: {sorted(offered)}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _send(self, content_type: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # the models list Codex may ask for
        self._send("application/json", json.dumps({"models": [], "data": []}).encode())

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        outputs = [item for item in request.get("input", []) if item.get("type") == "function_call_output"]
        with open(LOG, "a", encoding="utf-8") as log:
            if outputs:
                for item in outputs:
                    output = item.get("output")
                    log.write(f"OUTPUT: {output if isinstance(output, str) else json.dumps(output)}\n")
                message = {"type": "message", "role": "assistant", "id": "m1"}
                message["content"] = [{"type": "output_text", "text": "done"}]
                answer = {"type": "response.output_item.done", "item": message}
                body = _events({"type": "response.created", "response": {"id": "r2"}}, answer, _completed("r2"))
            else:
                name, arguments = _shell_call(request.get("tools", []))
                log.write(f"TOOL: {name}\n")
                call = {"type": "function_call", "name": name, "arguments": json.dumps(arguments), "call_id": "c1"}
                answer = {"type": "response.output_item.done", "item": call}
                body = _events({"type": "response.created", "response": {"id": "r1"}}, answer, _completed("r1"))
        self._send("text/event-stream", body)


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
