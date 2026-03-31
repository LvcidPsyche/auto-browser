#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
from typing import Any

if sys.version_info < (3, 11):
    raise SystemExit("auto-browser controller tooling requires Python 3.11+")

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
    ) -> None:
        self.codex_path = codex_path
        self.default_model = default_model
        self.request_timeout_seconds = request_timeout_seconds

    def handle_openai_decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("prompt") or "").strip()
        schema = payload.get("schema")
        screenshot = payload.get("screenshot")
        if not prompt:
            raise BridgeError("prompt is required", status_code=400)
        if not isinstance(schema, dict):
            raise BridgeError("schema must be a JSON object", status_code=400)
        if not isinstance(screenshot, dict):
            raise BridgeError("screenshot must be an object", status_code=400)

        model = str(payload.get("model") or self.default_model or "").strip() or None
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
            image_path.write_bytes(image_bytes)

            command = [self.codex_path]
            if model:
                command.extend(["--model", model])
            for override in CONFIG_OVERRIDES:
                command.extend(["-c", override])
            command.extend(
                [
                    "exec",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--skip-git-repo-check",
                    "--cd",
                    tempdir,
                    "--ephemeral",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "--image",
                    str(image_path),
                    "-",
                ]
            )

            try:
                result = subprocess.run(
                    command,
                    input=prompt.encode("utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=self.request_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise BridgeError(
                    f"codex timed out after {self.request_timeout_seconds:.0f}s",
                    status_code=504,
                ) from exc

            stdout = result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace")
            raw_text = output_path.read_text(encoding="utf-8") if output_path.exists() else stdout
            if result.returncode != 0:
                detail = (stderr or stdout or f"codex exited with {result.returncode}").strip()
                raise BridgeError(detail[:4000], status_code=502)
            if not raw_text.strip():
                raise BridgeError("codex returned an empty response", status_code=502)

            return {
                "model": model or self.default_model or "",
                "raw_text": raw_text,
                "stderr": stderr,
            }


class UnixHTTPServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: str, service: CodexBridgeService):
        self.service = service
        super().__init__(socket_path, BridgeRequestHandler)


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "AutoBrowserCodexHostBridge/0.1"
    protocol_version = "HTTP/1.1"

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
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        self._send_json(HTTPStatus.OK, response)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[codex-bridge] {format % args}", file=sys.stderr)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise BridgeError("request body must be a JSON object", status_code=400)
        return payload

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
    socket_path = Path(args.socket_path).resolve()
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()

    os.umask(0o077)
    service = CodexBridgeService(
        codex_path=args.codex_path,
        default_model=args.model,
        request_timeout_seconds=args.request_timeout_seconds,
    )
    server = UnixHTTPServer(str(socket_path), service)

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
