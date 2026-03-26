from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .mcp_transport import MCP_PROTOCOL_HEADER, MCP_SESSION_HEADER


@dataclass
class HttpMcpResponse:
    status_code: int
    headers: dict[str, str]
    body: dict[str, Any] | None


class HttpMcpClient:
    def __init__(self, *, base_url: str, bearer_token: str | None = None, timeout_seconds: float = 60.0):
        self.base_url = base_url
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds

    def post_json(
        self,
        payload: dict[str, Any],
        *,
        session_id: str | None = None,
        protocol_version: str | None = None,
    ) -> HttpMcpResponse:
        headers = {"Content-Type": "application/json"}
        if session_id:
            headers[MCP_SESSION_HEADER] = session_id
        if protocol_version:
            headers[MCP_PROTOCOL_HEADER] = protocol_version
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return self._request("POST", headers=headers, body=payload)

    def delete_session(self, *, session_id: str | None) -> None:
        if not session_id:
            return
        headers = {MCP_SESSION_HEADER: session_id}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        try:
            self._request("DELETE", headers=headers, body=None)
        except Exception:
            return

    def _request(self, method: str, *, headers: dict[str, str], body: dict[str, Any] | None) -> HttpMcpResponse:
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(self.base_url, data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                return HttpMcpResponse(
                    status_code=response.status,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=self._decode_json(raw),
                )
        except HTTPError as exc:
            return HttpMcpResponse(
                status_code=exc.code,
                headers={k.lower(): v for k, v in exc.headers.items()},
                body=self._decode_json(exc.read()),
            )

    @staticmethod
    def _decode_json(raw: bytes) -> dict[str, Any] | None:
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))


class StdioMcpBridge:
    def __init__(self, *, client: HttpMcpClient, stderr: TextIO | None = None):
        self.client = client
        self.stderr = stderr or sys.stderr
        self.session_id: str | None = None
        self.protocol_version: str | None = None

    def run(self, *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
        input_stream = stdin or sys.stdin
        output_stream = stdout or sys.stdout
        try:
            for raw_line in input_stream:
                line = raw_line.strip()
                if not line:
                    continue
                response = self._handle_line(line)
                if response is not None:
                    output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
                    output_stream.flush()
        finally:
            self.client.delete_session(session_id=self.session_id)
        return 0

    def _handle_line(self, line: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return self._jsonrpc_error(None, -32700, "Invalid JSON payload")

        if isinstance(payload, list):
            return self._jsonrpc_error(None, -32600, "JSON-RPC batches are not supported")
        if not isinstance(payload, dict):
            return self._jsonrpc_error(None, -32600, "JSON-RPC body must be an object")

        request_id = payload.get("id")
        try:
            response = self.client.post_json(
                payload,
                session_id=None if payload.get("method") == "initialize" else self.session_id,
                protocol_version=self.protocol_version,
            )
        except URLError as exc:
            return self._jsonrpc_error(request_id, -32000, f"Unable to reach Auto Browser MCP HTTP endpoint: {exc.reason}")
        except Exception as exc:  # pragma: no cover - defensive bridge guard
            print(f"stdio bridge unexpected error: {exc}", file=self.stderr)
            return self._jsonrpc_error(request_id, -32000, f"Unexpected stdio bridge failure: {exc}")

        next_session_id = response.headers.get(MCP_SESSION_HEADER.lower())
        if next_session_id:
            self.session_id = next_session_id

        next_protocol_version = response.headers.get(MCP_PROTOCOL_HEADER.lower())
        if next_protocol_version:
            self.protocol_version = next_protocol_version
        elif payload.get("method") == "initialize":
            result = (response.body or {}).get("result") if isinstance(response.body, dict) else None
            if isinstance(result, dict):
                version = result.get("protocolVersion")
                if isinstance(version, str) and version.strip():
                    self.protocol_version = version.strip()

        if payload.get("id") is None:
            return None
        if response.body is None:
            return self._jsonrpc_error(request_id, -32000, f"Empty response from Auto Browser MCP endpoint ({response.status_code})")
        return response.body

    @staticmethod
    def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message,
            },
        }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bridge stdio MCP clients to the Auto Browser HTTP MCP endpoint.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AUTO_BROWSER_BASE_URL", "http://127.0.0.1:8000/mcp"),
        help="HTTP MCP endpoint to proxy to (default: %(default)s)",
    )
    parser.add_argument(
        "--bearer-token",
        default=os.environ.get("AUTO_BROWSER_BEARER_TOKEN"),
        help="Optional API bearer token for the Auto Browser HTTP server.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.environ.get("AUTO_BROWSER_HTTP_TIMEOUT_SECONDS", "60")),
        help="Per-request timeout when talking to the HTTP MCP endpoint.",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    bridge = StdioMcpBridge(
        client=HttpMcpClient(
            base_url=args.base_url,
            bearer_token=args.bearer_token,
            timeout_seconds=args.timeout_seconds,
        )
    )
    return bridge.run()


if __name__ == "__main__":
    raise SystemExit(main())
