#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
from typing import Any

SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_SANDBOX_MODE = "read-only"

# A request is a prompt, a schema and at most one base64 viewport screenshot
# (a few MB; a noisy 4K PNG is ~15 MB as base64). Anything larger is not a
# legitimate request, and the body is read into memory before it is parsed.
MAX_REQUEST_BODY_BYTES = 32 * 1024 * 1024
# Seconds a client may stall between bytes of a request. Without it one idle or
# slow connection pinned a handler thread forever.
REQUEST_READ_TIMEOUT_SECONDS = 30.0
# Seconds a client may take to deliver the whole body. The per-read timeout above restarts on every byte, so a
# client sending one byte every 29 s would otherwise hold a handler thread for hours.
REQUEST_BODY_DEADLINE_SECONDS = 60.0
# Codex runs at once. Each is a heavy host process, and the socket has no other limit on concurrent requests.
MAX_CONCURRENT_DECISIONS = 2
# Seconds a request waits for one of those slots before it is answered 503.
DECISION_SLOT_WAIT_SECONDS = 2.0


def build_sandbox_flags(*, allow_host_exec: bool, sandbox_mode: str) -> list[str]:
    """Mirror of app/codex_sandbox.py — this script runs outside the container.

    Codex is handed a screenshot and a prompt and asked for a JSON decision; it
    is never meant to run shell commands here, so the default is a read-only
    sandbox and the bypass is an explicit opt-in (GHSA-xmh3-cw7j-9gp5).
    """
    if allow_host_exec:
        return ["--dangerously-bypass-approvals-and-sandbox"]
    mode = (sandbox_mode or DEFAULT_SANDBOX_MODE).strip() or DEFAULT_SANDBOX_MODE
    if mode not in SANDBOX_MODES:
        raise SystemExit(f"CODEX_SANDBOX_MODE must be one of {', '.join(SANDBOX_MODES)} (got {mode!r})")
    return ["-s", mode]


# Same pattern as MODEL_NAME_PATTERN in controller/app/models.py (this script ships without the controller; a test
# keeps the two equal). The leading alphanumeric keeps a model from being read as a codex flag.
MODEL_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_MODEL_NAME_RE = re.compile(MODEL_NAME_PATTERN)


# sockaddr_un.sun_path, terminator included: 104 on macOS and the BSDs, 108 on Linux.
AF_UNIX_PATH_MAX = 104 if sys.platform == "darwin" else 108


class SocketPathError(RuntimeError):
    pass


def prepare_socket_path(raw: str) -> Path:
    """The resolved path to bind, with a stale socket there removed; SocketPathError when it cannot be used.

    Only a socket is ever unlinked: the path used to be resolved and whatever file it named was deleted, so a
    --socket-path that pointed at a regular file or a symlink removed it (or the symlink's target).
    """
    socket_path = Path(raw).resolve()
    if len(os.fsencode(str(socket_path))) >= AF_UNIX_PATH_MAX:
        raise SocketPathError(
            f"socket path is too long for a unix socket ({len(os.fsencode(str(socket_path)))} bytes, "
            f"limit {AF_UNIX_PATH_MAX - 1}): {socket_path}; pass a shorter --socket-path"
        )
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (Path(raw), socket_path):
        try:
            mode = os.lstat(candidate).st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISSOCK(mode):
            raise SocketPathError(f"{candidate} exists and is not a socket; refusing to remove it")
    if socket_path.exists():
        socket_path.unlink()
    return socket_path


def model_name_is_valid(model: str) -> bool:
    return bool(_MODEL_NAME_RE.fullmatch(model)) and ".." not in model


CONFIG_OVERRIDES = (
    "project_doc_fallback_filenames=[]",
    "agents={}",
    "mcp_servers={}",
    "features.multi_agent=false",
    "features.apps=false",
    'web_search="disabled"',
)


@dataclass
class BridgeError(RuntimeError):
    message: str
    status_code: int = 500


class CodexBridgeService:
    def __init__(
        self,
        *,
        codex_path: str,
        default_model: str | None = None,
        request_timeout_seconds: float = 55.0,
        sandbox_mode: str = DEFAULT_SANDBOX_MODE,
        allow_host_exec: bool = False,
        max_concurrent_decisions: int = MAX_CONCURRENT_DECISIONS,
    ) -> None:
        self._decision_slots = threading.BoundedSemaphore(max(1, max_concurrent_decisions))
        self.codex_path = codex_path
        self.default_model = default_model
        self.request_timeout_seconds = request_timeout_seconds
        self.sandbox_flags = build_sandbox_flags(allow_host_exec=allow_host_exec, sandbox_mode=sandbox_mode)
        if allow_host_exec:
            print(
                "WARNING: CODEX_BRIDGE_ALLOW_HOST_EXEC is set. Codex runs with approvals and "
                "sandboxing disabled, so a model decision can execute arbitrary commands on this "
                "host. Only do this inside an environment that is itself sandboxed.",
                file=sys.stderr,
                flush=True,
            )

    def handle_openai_decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._decision_slots.acquire(timeout=DECISION_SLOT_WAIT_SECONDS):
            raise BridgeError("bridge is busy: too many decisions in flight", status_code=503)
        try:
            return self._decide(payload)
        finally:
            self._decision_slots.release()

    def _decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("prompt") or "").strip()
        schema = payload.get("schema")
        screenshot = payload.get("screenshot")
        if not prompt:
            raise BridgeError("prompt is required", status_code=400)
        if not isinstance(schema, dict):
            raise BridgeError("schema must be a JSON object", status_code=400)
        # Absent for a text-preset observation, which has no screenshot to send.
        if screenshot is not None and not isinstance(screenshot, dict):
            raise BridgeError("screenshot must be an object", status_code=400)

        model = str(payload.get("model") or self.default_model or "").strip() or None
        if model is not None and not model_name_is_valid(model):
            raise BridgeError("model must match " + MODEL_NAME_PATTERN + " and not contain '..'", status_code=400)
        image_bytes: bytes | None = None
        suffix = ".png"
        if screenshot is not None:
            media_type = str(screenshot.get("media_type") or "image/png").strip() or "image/png"
            image_base64 = str(screenshot.get("base64") or "").strip()
            if not image_base64:
                raise BridgeError("screenshot.base64 is required", status_code=400)

            try:
                image_bytes = base64.b64decode(image_base64, validate=True)
            except Exception as exc:  # pragma: no cover - defensive
                raise BridgeError(f"invalid screenshot.base64: {exc}", status_code=400) from exc

            suffix = mimetypes.guess_extension(media_type) or ".png"
        with tempfile.TemporaryDirectory(prefix="auto-browser-codex-") as tempdir:
            temp_root = Path(tempdir)
            schema_path = temp_root / "schema.json"
            output_path = temp_root / "decision.json"
            image_path = temp_root / f"screenshot{suffix}"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            if image_bytes is not None:
                image_path.write_bytes(image_bytes)

            command = [self.codex_path]
            if model:
                command.extend(["--model", model])
            for override in CONFIG_OVERRIDES:
                command.extend(["-c", override])
            command.extend(["exec", "--skip-git-repo-check"])
            command.extend(self.sandbox_flags)
            command.extend(
                [
                    "--cd",
                    tempdir,
                    "--ephemeral",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    *(["--image", str(image_path)] if image_bytes is not None else []),
                    "-",
                ]
            )

            returncode, stdout_bytes, stderr_bytes = self._run_codex(command, prompt.encode("utf-8"))
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            if stderr.strip():
                # Codex's stderr stays on the bridge side; the reply to the controller carries the decision only.
                print(f"[codex-bridge] codex stderr: {stderr.strip()[:2000]}", file=sys.stderr, flush=True)
            raw_text = output_path.read_text(encoding="utf-8") if output_path.exists() else stdout
            if returncode != 0:
                detail = (stderr or stdout or f"codex exited with {returncode}").strip()
                raise BridgeError(detail[:4000], status_code=502)
            if not raw_text.strip():
                raise BridgeError("codex returned an empty response", status_code=502)

            return {
                "model": model or self.default_model or "",
                "raw_text": raw_text,
            }

    def _run_codex(self, command: list[str], prompt: bytes) -> tuple[int, bytes, bytes]:
        """Run codex with `prompt` on stdin; on timeout kill its whole process group, not just the direct child."""
        grouped = hasattr(os, "killpg")  # POSIX; the bridge listens on a Unix socket, so this is its normal case
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=grouped,
        )
        try:
            stdout, stderr = process.communicate(prompt, timeout=self.request_timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _kill_process_tree(process, grouped)
            process.communicate()
            raise BridgeError(
                f"codex timed out after {self.request_timeout_seconds:.0f}s",
                status_code=504,
            ) from exc
        except BaseException:
            _kill_process_tree(process, grouped)
            process.communicate()
            raise
        return process.returncode, stdout, stderr


def _kill_process_tree(process: subprocess.Popen, grouped: bool) -> None:
    try:
        if grouped:
            os.killpg(process.pid, signal.SIGKILL)  # the group codex leads (start_new_session): its children too
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass  # already gone


class UnixHTTPServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: str, service: CodexBridgeService):
        self.service = service
        super().__init__(socket_path, BridgeRequestHandler)


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "AutoBrowserCodexHostBridge/0.1"
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_READ_TIMEOUT_SECONDS

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/healthz":
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Not found"})
            return
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/openai/decide":
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Not found"})
            return

        try:
            payload = self._read_json_body()
            response = self.server.service.handle_openai_decide(payload)  # type: ignore[attr-defined]
        except BridgeError as exc:
            self._send_json(exc.status_code, {"error": exc.message})
            return
        except json.JSONDecodeError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid json: {exc}"})
            return
        except UnicodeDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request body must be UTF-8 encoded JSON"})
            return
        except TimeoutError:
            self.close_connection = True
            self._send_json(HTTPStatus.REQUEST_TIMEOUT, {"error": "timed out reading the request body"})
            return
        except Exception:
            # The detail goes to the bridge's log, not to the caller.
            traceback.print_exc(file=sys.stderr)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})
            return

        self._send_json(HTTPStatus.OK, response)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[codex-bridge] {format % args}", file=sys.stderr)

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            # rfile.read(-1) would read until the client closes the connection.
            self.close_connection = True
            raise BridgeError("invalid Content-Length", status_code=400)
        if length > MAX_REQUEST_BODY_BYTES:
            # The body stays unread, so the connection cannot be reused for another request.
            self.close_connection = True
            raise BridgeError(
                f"request body of {length} bytes exceeds the {MAX_REQUEST_BODY_BYTES} byte limit",
                status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        body = self._read_exactly(length)
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise BridgeError("request body must be a JSON object", status_code=400)
        return payload

    def _read_exactly(self, length: int) -> bytes:
        """`length` bytes of the body, within REQUEST_BODY_DEADLINE_SECONDS in total (TimeoutError past it)."""
        deadline = time.monotonic() + REQUEST_BODY_DEADLINE_SECONDS
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("request body not delivered in time")
            self.connection.settimeout(min(left, self.timeout))
            chunk = self.rfile.read1(min(remaining, 65536))
            if not chunk:
                self.close_connection = True
                raise BridgeError("request body shorter than its Content-Length", status_code=400)
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Host-side Codex bridge for Auto Browser")
    parser.add_argument(
        "--socket-path",
        default="data/host-bridge/codex.sock",
        help="Unix socket path to listen on",
    )
    parser.add_argument(
        "--codex-path",
        default="codex",
        help="Path to the host codex CLI",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional default model override",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=55.0,
        help="Kill host codex requests that exceed this timeout",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        socket_path = prepare_socket_path(args.socket_path)
    except SocketPathError as exc:
        print(f"[codex-bridge] {exc}", file=sys.stderr)
        return 2

    os.umask(0o077)
    service = CodexBridgeService(
        codex_path=args.codex_path,
        default_model=args.model,
        request_timeout_seconds=args.request_timeout_seconds,
        sandbox_mode=os.environ.get("CODEX_SANDBOX_MODE", DEFAULT_SANDBOX_MODE),
        allow_host_exec=os.environ.get("CODEX_BRIDGE_ALLOW_HOST_EXEC", "").strip().lower()
        in {"1", "true", "yes", "on"},
    )
    server = UnixHTTPServer(str(socket_path), service)
    # The umask above already makes it 0600; say so explicitly rather than rely on it.
    os.chmod(socket_path, 0o600)

    def shutdown(_: int, __: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"[codex-bridge] listening on {socket_path}", file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if socket_path.exists():
            socket_path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
