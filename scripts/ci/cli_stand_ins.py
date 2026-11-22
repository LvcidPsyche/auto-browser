"""Stand-ins for the checks of scripts/ci that run a provider CLI end to end (codex_bridge_mcp_check.py,
codex_adapter_check.py, claude_adapter_check.py): a model on loopback for each CLI, which logs the tools each request
offers, a stdio MCP server that notes when a CLI starts it, a telemetry listener, a program that notes its run, a Node
that notes its run and a language server that notes its run; and the checks' reading of those notes. Loopback only; no
network needed.

  cli_stand_ins.py responses PORT LOG [TARGET ...]
                                        a Responses API model (Codex's): calls the first MCP tool it is offered,
                                        once; given TARGETs, where a request offers apply_patch, sends a patch that
                                        adds the next TARGET, one per request, each as the request before it came
                                        back with the last patch's output; then answers DECISION
  cli_stand_ins.py messages PORT LOG    a Messages API model (Claude Code's): calls the first MCP tool it is offered,
                                        once, then the LSP tool where it is offered, once, on LSP_FILE, then answers
                                        DECISION through the structured-output tool where one is offered, else as text
  cli_stand_ins.py server LABEL FILE    a stdio MCP server that appends "LABEL started" to FILE
  cli_stand_ins.py listen PORT LOG      an HTTP server that appends "METHOD PATH" of each request to LOG
  cli_stand_ins.py mark LABEL FILE ...  a program that appends "LABEL ran" to FILE (further arguments ignored)
  cli_stand_ins.py node LABEL FILE ...  a Node that appends "LABEL ran" to FILE and answers --version with
                                        NODE_VERSION, a Node that Codex's js_repl and code_mode take
  cli_stand_ins.py lsp LABEL FILE       a stdio language server that appends "LABEL ran" to FILE and answers
                                        `initialize` with its capabilities and each other request with null

Each model appends one JSON line per request to LOG: {"tools": [the names offered], "answer": what it answered,
"body": the request's body as sent}; the Responses API model's line also holds "patched": what Codex answered each of
its patches with, in order, as the request holds them. Each model logs a tool by its name, else its type, and as null
where the request names it by neither; a tool that holds tools (a namespace) is logged as that, and each tool it holds
after it as "<its name>/<the held tool's name>".
"""

from __future__ import annotations

import json
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NoReturn

DECISION = {"action": "done", "reason": "stand-in", "risk_category": "read"}
_MCP_PREFIX = "mcp__"
# The one tool of the stand-in MCP server.
SERVER_TOOL = "write_file"
# Codex's file-edit tool, which the Responses API model calls where it is given targets; and what a patch it sends adds
# to the file it names.
PATCH_TOOL = "apply_patch"
PATCHED_TEXT = "written by the stand-in model"
# The tool through which Claude Code's --json-schema has the model answer.
STRUCTURED_OUTPUT = "StructuredOutput"
# Claude Code's tool for a plugin's language servers, and the file the messages model's call of it names, relative to
# the CLI's working directory.
LSP_TOOL = "LSP"
LSP_FILE = "planted.py"
# What the stand-in Node answers to --version.
NODE_VERSION = "v22.22.0"
# How long a CLI call of a check's may take (and each `claude plugin` call of its planting).
CALL_TIMEOUT_SECONDS = 180.0
# How long a stand-in gets to answer on its port before the check fails; and how long a control, or a decision of the
# Codex adapter check's settings case, waits for the notes it awaits (wait_for), after which its verdicts read the
# notes and fail where one did not come.
START_SECONDS = 60.0
NOTES_SECONDS = 60.0
POLL_SECONDS = 0.1
# How long after a decision's call returned its notes are read: SETTLE_SECONDS, or SETTLE_FACTOR times as long as its
# control's call took, from its return, to have every note it awaited there, whichever is longer. A planted program the
# CLI starts and does not wait for notes its run after the call returned, and that none ran cannot be waited for.
SETTLE_SECONDS = 5.0
SETTLE_FACTOR = 3


def fail(message: str) -> NoReturn:
    print(f"FAIL: {message}")
    raise SystemExit(1)


class Notes:
    """Where a check's stand-ins note what happened, in ``work``: each planted MCP server's start (marker), each
    request the model got (model_log), each run of a planted program (extras) and each request the listener got
    (telemetry)."""

    def __init__(self, work: Path) -> None:
        self.work = work
        self.marker, self.model_log = work / "mcp-started", work / "model.log"
        self.extras, self.telemetry = work / "extras-ran", work / "telemetry.log"

    def fresh(self) -> None:
        for path in (self.marker, self.model_log, self.extras, self.telemetry):
            path.unlink(missing_ok=True)


def noted(path: Path) -> list[str]:
    """The lines a stand-in noted in ``path``, sorted; none where it noted nothing."""
    return sorted(path.read_text(encoding="utf-8").splitlines()) if path.exists() else []


def wait_for(expected: dict[Path, Iterable[str]]) -> float:
    """Returns once each file holds every line ``expected`` names for it, or NOTES_SECONDS on, whichever comes first:
    how long it waited. A control, and a decision of the Codex adapter check's settings case, waits so, from its
    call's return, for what its call starts and runs; its verdicts then read the notes, and fail where a line did not
    come."""
    began = time.monotonic()
    deadline = began + NOTES_SECONDS
    while time.monotonic() < deadline and not all(set(lines) <= set(noted(path)) for path, lines in expected.items()):
        time.sleep(POLL_SECONDS)
    return time.monotonic() - began


def settle_seconds(landed: float) -> float:
    """How long after a decision's call returned its notes are read, where every note its control awaited was there
    ``landed`` seconds after the control's call returned (0 where all were there at the return); printed in one
    line."""
    seconds = max(SETTLE_SECONDS, SETTLE_FACTOR * landed)
    print(
        f"every awaited note was there {landed:.1f} s after the control's call returned; a decision waits "
        f"{seconds:.1f} s for its notes"
    )
    return seconds


def _sse(*events: dict) -> bytes:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


def _tool_names(tools: list[dict]) -> list[str | None]:
    """``tools`` as a model logs them: each by its name (an empty one too), else its type (None where it has neither),
    and after a tool that holds tools (a namespace) each of those as "<its name>/<the held tool's name>"."""
    names: list[str | None] = []
    for tool in tools:
        name = tool.get("name")
        if name is None:
            name = tool.get("type")
        names += [name, *(f"{name}/{held}" for held in _tool_names(tool.get("tools") or []))]
    return names


def _log(log: str, tools: list, answer: str, body: str, **more: object) -> None:
    with open(log, "a", encoding="utf-8") as out:
        out.write(json.dumps({"tools": tools, "answer": answer, "body": body, **more}) + "\n")


def logged(log: Path) -> list[dict]:
    """The requests a model logged, oldest first: each {"tools": [...], "answer": ..., "body": ...}, and the Responses
    API model's "patched": [...]."""
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def patched(requests: list[dict]) -> list[str]:
    """What Codex answered each patch of the Responses API model's with, in order, as the last of ``requests`` holds
    it; none where the model was not asked."""
    return requests[-1].get("patched", []) if requests else []


def _patch(target: str) -> str:
    """The patch the Responses API model sends for ``target``: one that adds that file, holding PATCHED_TEXT."""
    return f"*** Begin Patch\n*** Add File: {target}\n+{PATCHED_TEXT}\n*** End Patch\n"


def offered(requests: list[dict]) -> list[str | None]:
    """The tools offered in any of ``requests``, each once, sorted; one logged with no name is None, first."""
    return sorted(
        {name for request in requests for name in request["tools"]}, key=lambda name: (name is not None, name)
    )


def mcp_tools(requests: list[dict]) -> list[str]:
    """The MCP tools offered in any of ``requests``, each once, sorted."""
    return [name for name in offered(requests) if str(name).startswith(_MCP_PREFIX)]


def server_tool(server: str) -> str:
    """The stand-in MCP server's tool as a CLI offers it to the model, for the server the CLI calls ``server``."""
    return f"{_MCP_PREFIX}{server}__{SERVER_TOOL}"


def offered_exactly(requests: list[dict], names: tuple[str, ...]) -> None:
    """Fails unless the tools offered in ``requests`` are ``names`` and no other, naming each other tool offered and
    each of ``names`` not offered."""
    tools = offered(requests)
    if others := [name for name in tools if name not in names]:
        fail(f"the model was offered a tool besides {', '.join(names)}: {others}")
    if missing := [name for name in names if name not in tools]:
        fail(f"the model was not offered {', '.join(missing)}: {tools}")


def held(requests: list[dict], marks: Iterable[str]) -> list[str]:
    """Those of ``marks`` that a body of ``requests`` holds, in ``marks``' order."""
    return [mark for mark in marks if any(mark in request["body"] for request in requests)]


class _Handler(BaseHTTPRequestHandler):
    log_path = ""

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, content_type: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _request(self) -> tuple[str, dict]:
        """The request's body as sent, and as JSON."""
        body = self.rfile.read(int(self.headers.get("content-length", 0))).decode("utf-8", "replace")
        return body, json.loads(body or "{}")


class _Responses(_Handler):
    targets: tuple[str, ...] = ()

    def do_GET(self) -> None:  # the models list Codex may ask for
        self._send("application/json", json.dumps({"models": [], "data": []}).encode())

    def do_POST(self) -> None:
        body, request = self._request()
        items = request.get("input", [])
        answered = any(item.get("type") == "function_call_output" for item in items)
        patched = [str(item.get("output")) for item in items if item.get("type") == "custom_tool_call_output"]
        tools = _tool_names(request.get("tools", []) or [])
        mcp = [name for name in tools if str(name).startswith(_MCP_PREFIX)]
        if mcp and not answered:
            item = {"type": "function_call", "name": mcp[0], "arguments": "{}", "call_id": "c1"}
        elif PATCH_TOOL in tools and len(patched) < len(self.targets):
            patch = _patch(self.targets[len(patched)])
            item = {"type": "custom_tool_call", "name": PATCH_TOOL, "input": patch, "call_id": f"p{len(patched)}"}
        else:
            content = [{"type": "output_text", "text": json.dumps(DECISION)}]
            item = {"type": "message", "role": "assistant", "id": "m1", "content": content}
        _log(self.log_path, tools, item.get("name", "text"), body, patched=patched)
        usage = {"input_tokens": 0, "input_tokens_details": None, "output_tokens": 0, "output_tokens_details": None}
        completed = {"type": "response.completed", "response": {"id": "r1", "usage": {**usage, "total_tokens": 0}}}
        answer = {"type": "response.output_item.done", "item": item}
        self._send("text/event-stream", _sse({"type": "response.created", "response": {"id": "r1"}}, answer, completed))


def _message(blocks: list[dict], stop: str | None) -> dict:
    return {
        "id": "msg_stand_in",
        "type": "message",
        "role": "assistant",
        "model": "stand-in",
        "content": blocks,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _message_stream(blocks: list[dict], stop: str) -> bytes:
    events = [{"type": "message_start", "message": _message([], None)}]
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            start, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": block["text"]}
        else:
            arguments = json.dumps(block["input"])
            start, delta = dict(block, input={}), {"type": "input_json_delta", "partial_json": arguments}
        events += [
            {"type": "content_block_start", "index": index, "content_block": start},
            {"type": "content_block_delta", "index": index, "delta": delta},
            {"type": "content_block_stop", "index": index},
        ]
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return _sse(*events)


class _Messages(_Handler):
    def do_GET(self) -> None:
        self._send("application/json", b"{}")

    def do_POST(self) -> None:
        body, request = self._request()
        if "count_tokens" in self.path:
            self._send("application/json", b'{"input_tokens": 1}')
            return
        tools = _tool_names(request.get("tools", []) or [])
        results = {
            block.get("tool_use_id"): str(block.get("content"))
            for message in request.get("messages", [])
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "tool_result"
        }
        mcp = [name for name in tools if str(name).startswith(_MCP_PREFIX)]
        if mcp and not results:
            blocks, stop = [{"type": "tool_use", "id": "toolu_1", "name": mcp[0], "input": {}}], "tool_use"
        elif LSP_TOOL in tools and "toolu_3" not in results:
            hover = {"operation": "hover", "filePath": LSP_FILE, "line": 1, "character": 1}
            blocks, stop = [{"type": "tool_use", "id": "toolu_3", "name": LSP_TOOL, "input": hover}], "tool_use"
        elif STRUCTURED_OUTPUT in tools and not any("Structured output" in result for result in results.values()):
            blocks = [{"type": "tool_use", "id": "toolu_2", "name": STRUCTURED_OUTPUT, "input": DECISION}]
            stop = "tool_use"
        else:
            blocks, stop = [{"type": "text", "text": json.dumps(DECISION)}], "end_turn"
        _log(self.log_path, tools, blocks[0].get("name", "text"), body)
        if request.get("stream"):
            self._send("text/event-stream", _message_stream(blocks, stop))
        else:
            self._send("application/json", json.dumps(_message(blocks, stop)).encode())


class _Listener(_Handler):
    def _any(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0)))
        with open(self.log_path, "a", encoding="utf-8") as out:
            out.write(f"{self.command} {self.path}\n")
        self._send("application/json", b"{}")

    do_GET = do_POST = _any


def _serve(handler: type[_Handler], port: int, log: str, **attributes: object) -> None:
    served = type(handler.__name__, (handler,), {"log_path": log, **attributes})
    ThreadingHTTPServer(("127.0.0.1", port), served).serve_forever()


def _this_file(role: str, *args: str) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), role, *args]


def _answers(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), 1).close()
    except OSError:
        return False
    return True


@contextmanager
def running(role: str, port: int, log: Path, *targets: str) -> Iterator[None]:
    """This file's ``role`` (a model, or the listener), answering on ``port`` for the block, the responses model given
    ``targets``; killed after it, and waited for. The check fails, naming the role, where it ends before it answers
    (with its exit code) or does not answer in START_SECONDS."""
    what = "listener" if role == "listen" else f"{role} model"
    child = subprocess.Popen(_this_file(role, str(port), str(log), *targets))
    try:
        deadline = time.monotonic() + START_SECONDS
        while (code := child.poll()) is None and not _answers(port):
            if time.monotonic() >= deadline:
                fail(f"the stand-in {what} did not start")
            time.sleep(POLL_SECONDS)
        if code is not None:
            fail(f"the stand-in {what} ended with exit code {code} before it answered on port {port}")
        yield
    finally:
        child.kill()
        child.wait()


def server_command(label: str, marker: Path) -> list[str]:
    """The command line of a planted server: this file's stdio MCP server, noting its start in ``marker``."""
    return _this_file("server", label, str(marker))


def mark_command(label: str, marker: Path) -> list[str]:
    """The command line of a planted program (a notify program, a hook): this file's mark, noting its run in
    ``marker``."""
    return _this_file("mark", label, str(marker))


def _program(role: str, label: str, marker: Path, path: Path) -> Path:
    path.write_text(f'#!/bin/sh\nexec {shlex.join(_this_file(role, label, str(marker)))} "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def node_program(label: str, marker: Path, path: Path) -> Path:
    """Writes at ``path`` a planted Node, for a key that names a program and no arguments (js_repl_node_path): a shell
    script that runs this file's node with the arguments it is given, noting its run in ``marker``. Returns ``path``."""
    return _program("node", label, marker, path)


def mark_program(label: str, marker: Path, path: Path) -> Path:
    """Writes at ``path`` a planted program for a key that names a program and no arguments (zsh_path): a shell script
    that runs this file's mark, noting its run in ``marker``. Returns ``path``."""
    return _program("mark", label, marker, path)


def language_server_command(label: str, marker: Path) -> list[str]:
    """The command line of a planted language server: this file's, noting its run in ``marker``."""
    return _this_file("lsp", label, str(marker))


def _language_server(label: str, marker: str) -> None:
    with open(marker, "a", encoding="utf-8") as out:
        out.write(f"{label} ran\n")
    while True:
        length = 0
        while line := sys.stdin.buffer.readline():
            if not line.strip():
                break
            name, _, value = line.decode("ascii", "replace").partition(":")
            length = int(value) if name.strip().lower() == "content-length" else length
        if not line:
            return
        message = json.loads(sys.stdin.buffer.read(length) or "{}")
        if message.get("method") == "exit":
            return
        if "id" not in message:
            continue
        result = {"capabilities": {"hoverProvider": True}} if message.get("method") == "initialize" else None
        body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
        sys.stdout.buffer.flush()


def _server(label: str, marker: str) -> None:
    with open(marker, "a", encoding="utf-8") as out:
        out.write(f"{label} started\n")
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": message.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": label, "version": "0"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": SERVER_TOOL, "inputSchema": {"type": "object", "properties": {}}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "written"}]}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    role = sys.argv[1:2]
    if role == ["responses"]:
        _serve(_Responses, int(sys.argv[2]), sys.argv[3], targets=tuple(sys.argv[4:]))
    elif role == ["messages"]:
        _serve(_Messages, int(sys.argv[2]), sys.argv[3])
    elif role == ["server"]:
        _server(sys.argv[2], sys.argv[3])
    elif role == ["listen"]:
        _serve(_Listener, int(sys.argv[2]), sys.argv[3])
    elif role == ["lsp"]:
        _language_server(sys.argv[2], sys.argv[3])
    elif role == ["mark"]:
        with open(sys.argv[3], "a", encoding="utf-8") as out:
            out.write(f"{sys.argv[2]} ran\n")
    elif role == ["node"]:
        with open(sys.argv[3], "a", encoding="utf-8") as out:
            out.write(f"{sys.argv[2]} ran\n")
        if "--version" in sys.argv[4:]:
            print(NODE_VERSION)
    else:
        raise SystemExit(__doc__)
