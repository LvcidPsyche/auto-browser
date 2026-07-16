from __future__ import annotations

import email.message
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from auto_browser_client.mcp_bridge import (
    MCP_SESSION_HEADER,
    HttpMcpClient,
    HttpMcpResponse,
    StdioMcpBridge,
    build_arg_parser,
)


class RecordingHttpMcpClient:
    """Configurable fake honoring the HttpMcpClient interface."""

    def __init__(self, response: HttpMcpResponse | Exception):
        self.response = response
        self.posts: list[dict[str, object]] = []
        self.deleted_session_ids: list[str | None] = []

    def post_json(self, payload, *, session_id=None, protocol_version=None):
        self.posts.append(
            {"payload": payload, "session_id": session_id, "protocol_version": protocol_version}
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def delete_session(self, *, session_id=None):
        self.deleted_session_ids.append(session_id)


def _run_line(bridge: StdioMcpBridge, line: str) -> dict | None:
    stdout = io.StringIO()
    bridge.run(stdin=io.StringIO(line + "\n"), stdout=stdout)
    raw = stdout.getvalue().strip()
    return json.loads(raw) if raw else None


class BridgeProtocolEdgeTests(unittest.TestCase):
    def _ok_response(self) -> HttpMcpResponse:
        return HttpMcpResponse(status_code=200, headers={}, body={"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_batch_payload_rejected(self) -> None:
        bridge = StdioMcpBridge(client=RecordingHttpMcpClient(self._ok_response()))
        payload = _run_line(bridge, json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "x"}]))
        self.assertEqual(payload["error"]["code"], -32600)
        self.assertIn("batches", payload["error"]["message"])

    def test_non_object_payload_rejected(self) -> None:
        bridge = StdioMcpBridge(client=RecordingHttpMcpClient(self._ok_response()))
        payload = _run_line(bridge, '"just a string"')
        self.assertEqual(payload["error"]["code"], -32600)

    def test_notification_without_id_produces_no_output(self) -> None:
        client = RecordingHttpMcpClient(self._ok_response())
        bridge = StdioMcpBridge(client=client)
        payload = _run_line(bridge, json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(payload)
        self.assertEqual(len(client.posts), 1)

    def test_unreachable_endpoint_maps_to_jsonrpc_error(self) -> None:
        client = RecordingHttpMcpClient(URLError("connection refused"))
        bridge = StdioMcpBridge(client=client)
        payload = _run_line(bridge, json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}))
        self.assertEqual(payload["id"], 7)
        self.assertEqual(payload["error"]["code"], -32000)
        self.assertIn("Unable to reach", payload["error"]["message"])

    def test_empty_body_with_id_maps_to_error_with_status(self) -> None:
        client = RecordingHttpMcpClient(HttpMcpResponse(status_code=204, headers={}, body=None))
        bridge = StdioMcpBridge(client=client)
        payload = _run_line(bridge, json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}))
        self.assertEqual(payload["error"]["code"], -32000)
        self.assertIn("204", payload["error"]["message"])

    def test_protocol_version_falls_back_to_initialize_result_body(self) -> None:
        client = RecordingHttpMcpClient(
            HttpMcpResponse(
                status_code=200,
                headers={MCP_SESSION_HEADER.lower(): "s-1"},
                body={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-25"}},
            )
        )
        bridge = StdioMcpBridge(client=client)
        _run_line(bridge, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
        self.assertEqual(bridge.protocol_version, "2025-11-25")
        self.assertEqual(bridge.session_id, "s-1")


class HttpMcpClientTests(unittest.TestCase):
    def _fake_urlopen(self, captured: dict, *, status: int = 200, body: bytes = b'{"ok": true}'):
        class FakeResponse:
            def __init__(self) -> None:
                self.status = status
                self.headers = {"MCP-Session-Id": "s-9"}

            def read(self) -> bytes:
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

        def fake(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

        return fake

    def test_post_json_sends_bearer_session_and_protocol_headers(self) -> None:
        captured: dict = {}
        client = HttpMcpClient(base_url="http://ctrl.test/mcp", bearer_token="sekret", timeout_seconds=5.0)
        with patch("auto_browser_client.mcp_bridge.urlopen", self._fake_urlopen(captured)):
            response = client.post_json(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                session_id="s-1",
                protocol_version="2025-11-25",
            )
        request = captured["request"]
        self.assertEqual(request.get_header("Authorization"), "Bearer sekret")
        # urllib normalizes header names via str.capitalize()
        self.assertEqual(request.get_header(MCP_SESSION_HEADER.capitalize()), "s-1")
        self.assertEqual(request.get_header("Mcp-protocol-version"), "2025-11-25")
        self.assertEqual(captured["timeout"], 5.0)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers, {"mcp-session-id": "s-9"})
        self.assertEqual(response.body, {"ok": True})

    def test_http_error_is_returned_as_response_not_raised(self) -> None:
        headers = email.message.Message()
        headers["Content-Type"] = "application/json"
        error = HTTPError(
            "http://ctrl.test/mcp", 401, "Unauthorized", headers, io.BytesIO(b'{"detail": "no"}')
        )

        def fake(request, timeout=None):
            raise error

        client = HttpMcpClient(base_url="http://ctrl.test/mcp")
        with patch("auto_browser_client.mcp_bridge.urlopen", fake):
            response = client.post_json({"jsonrpc": "2.0", "id": 1, "method": "x"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.body, {"detail": "no"})

    def test_delete_session_is_noop_without_session_id(self) -> None:
        client = HttpMcpClient(base_url="http://ctrl.test/mcp")
        with patch("auto_browser_client.mcp_bridge.urlopen") as fake:
            client.delete_session(session_id=None)
        fake.assert_not_called()

    def test_delete_session_swallows_transport_errors(self) -> None:
        client = HttpMcpClient(base_url="http://ctrl.test/mcp")
        with patch("auto_browser_client.mcp_bridge.urlopen", side_effect=URLError("down")):
            client.delete_session(session_id="s-1")  # must not raise


class ArgParserEnvTests(unittest.TestCase):
    def test_env_vars_override_defaults(self) -> None:
        env = {
            "AUTO_BROWSER_BASE_URL": "http://remote:9000/mcp",
            "AUTO_BROWSER_BEARER_TOKEN": "tok",
            "AUTO_BROWSER_HTTP_TIMEOUT_SECONDS": "12.5",
        }
        with patch.dict(os.environ, env):
            args = build_arg_parser().parse_args([])
        self.assertEqual(args.base_url, "http://remote:9000/mcp")
        self.assertEqual(args.bearer_token, "tok")
        self.assertEqual(args.timeout_seconds, 12.5)

    def test_cli_flags_override_env(self) -> None:
        with patch.dict(os.environ, {"AUTO_BROWSER_BASE_URL": "http://remote:9000/mcp"}):
            args = build_arg_parser().parse_args(["--base-url", "http://cli:1/mcp"])
        self.assertEqual(args.base_url, "http://cli:1/mcp")


if __name__ == "__main__":
    unittest.main()
