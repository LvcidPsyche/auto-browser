"""Stdio <-> HTTP bridge for Auto Browser's MCP endpoint.

Single-file and stdlib-only by design. This module ships in two places:
as ``auto_browser_client.mcp_bridge`` (the PyPI ``auto-browser-mcp`` console
script) and as ``app.mcp_stdio`` inside the controller image (Glama/Docker
entrypoint). A guard test asserts the two copies stay byte-identical, so any
edit here must be mirrored to the other copy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# MCP spec header names, kept as local constants so the bridge has no
# controller dependency. The controller test suite asserts these match
# app.mcp_transport's values.
MCP_SESSION_HEADER = "MCP-Session-Id"
MCP_PROTOCOL_HEADER = "MCP-Protocol-Version"


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
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            # A proxy's HTML error page, say. Reported with its status below.
            return None


class StdioMcpBridge:
    def __init__(self, *, client: HttpMcpClient, stderr: TextIO | None = None):
        self.client = client
        self.stderr = stderr or sys.stderr
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        # The client's initialize request, replayed if the controller forgets
        # the session (restart without a session store, or eviction).
        self._initialize_payload: dict[str, Any] | None = None
        self._initialized_sent = False

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
        method = payload.get("method")
        if method == "initialize":
            self._initialize_payload = payload
            self._initialized_sent = False
        try:
            response = self.client.post_json(
                payload,
                session_id=None if method == "initialize" else self.session_id,
                protocol_version=self.protocol_version,
            )
            if response.status_code == 404 and method != "initialize" and self._reinitialize():
                # A stdio client never sees the 404 as "re-initialize": to it the
                # server process is alive, so every later call failed with
                # "Unknown MCP session" until the client itself was restarted.
                response = self.client.post_json(
                    payload,
                    session_id=self.session_id,
                    protocol_version=self.protocol_version,
                )
            if method == "notifications/initialized":
                self._initialized_sent = True
        except URLError as exc:
            return self._jsonrpc_error(
                request_id,
                -32000,
                f"No Auto Browser controller reachable at {self.client.base_url} — "
                "start one with 'docker compose up -d' in the auto-browser repo, or pass "
                f"--base-url/AUTO_BROWSER_BASE_URL for a remote deployment. (underlying: {exc.reason})",
            )
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
        if not isinstance(response.body, dict) or "jsonrpc" not in response.body:
            # The HTTP layer's own errors (401 bad token, 400 missing operator
            # header, 429) are {"detail": ...}, not JSON-RPC. Relayed as-is
            # they carried no id, so the client never saw a response to its
            # request and hung without learning why.
            return self._jsonrpc_error(request_id, -32000, self._http_error_message(response))
        return response.body

    @staticmethod
    def _http_error_message(response: HttpMcpResponse) -> str:
        if response.body is None:
            return f"Empty or non-JSON response from Auto Browser MCP endpoint ({response.status_code})"
        detail = response.body.get("detail") if isinstance(response.body, dict) else None
        message = f"Auto Browser controller returned HTTP {response.status_code}"
        if detail:
            message += f": {detail}"
        if response.status_code == 401:
            message += " (set --bearer-token or AUTO_BROWSER_BEARER_TOKEN to the controller's API token)"
        elif response.status_code == 400 and "operator" in str(detail).lower():
            message += (
                " (this controller requires an operator id on every request: give the bridge one if it"
                " supports it, or turn off REQUIRE_OPERATOR_ID on the controller)"
            )
        elif response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            message += f" (rate limited; retry after {retry_after}s)" if retry_after else " (rate limited)"
        return message

    def _reinitialize(self) -> bool:
        """Open a fresh controller session with the client's own initialize.

        Returns whether the session was re-established; if not, the caller
        relays the original 404.
        """
        if self._initialize_payload is None or self.session_id is None:
            return False
        replay = {**self._initialize_payload, "id": "auto-browser-bridge-reinitialize"}
        response = self.client.post_json(replay, session_id=None, protocol_version=None)
        next_session_id = response.headers.get(MCP_SESSION_HEADER.lower())
        if response.status_code != 200 or not next_session_id:
            return False
        print("stdio bridge: controller forgot the MCP session; re-initialized", file=self.stderr)
        self.session_id = next_session_id
        self.protocol_version = response.headers.get(MCP_PROTOCOL_HEADER.lower()) or self.protocol_version
        if self._initialized_sent:
            self.client.post_json(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                session_id=self.session_id,
                protocol_version=self.protocol_version,
            )
        return True

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
