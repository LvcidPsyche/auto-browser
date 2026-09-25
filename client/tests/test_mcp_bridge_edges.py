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

    def __init__(self, response: HttpMcpResponse | Exception, *, base_url: str = "http://ctrl.test/mcp"):
        self.response = response
        self.base_url = base_url
        self.posts: list[dict[str, object]] = []
        self.deleted_session_ids: list[str | None] = []

    def post_json(self, payload, *, session_id=None, protocol_version=None):
        self.posts.append({"payload": payload, "session_id": session_id, "protocol_version": protocol_version})
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

    def test_unknown_session_is_relayed_when_reinitialize_fails(self) -> None:
        not_found = HttpMcpResponse(
            status_code=404, headers={}, body={"jsonrpc": "2.0", "id": 2, "error": {"code": -32001}}
        )
        client = RecordingHttpMcpClient(not_found)
        bridge = StdioMcpBridge(client=client, stderr=io.StringIO())
        bridge.session_id = "gone"
        bridge._initialize_payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}

        payload = _run_line(bridge, json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))

        self.assertEqual(payload["error"]["code"], -32001)
        self.assertEqual([post["payload"]["method"] for post in client.posts], ["tools/list", "initialize"])

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
        client = RecordingHttpMcpClient(URLError("connection refused"), base_url="http://ctrl.test/mcp")
        bridge = StdioMcpBridge(client=client)
        payload = _run_line(bridge, json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}))
        self.assertEqual(payload["id"], 7)
        self.assertEqual(payload["error"]["code"], -32000)
        message = payload["error"]["message"]
        self.assertIn("http://ctrl.test/mcp", message)
        self.assertIn("docker compose up -d", message)
        self.assertIn("--base-url/AUTO_BROWSER_BASE_URL", message)

    def test_empty_body_with_id_maps_to_error_with_status(self) -> None:
        client = RecordingHttpMcpClient(HttpMcpResponse(status_code=204, headers={}, body=None))
        bridge = StdioMcpBridge(client=client)
        payload = _run_line(bridge, json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}))
        self.assertEqual(payload["error"]["code"], -32000)
        self.assertIn("204", payload["error"]["message"])

    def test_http_layer_errors_become_jsonrpc_errors_with_the_request_id(self) -> None:
        """401/400/429 bodies are {"detail": ...}; relayed as-is they had no
        id, so a stdio client never got an answer and hung."""
        cases = [
            (401, {}, {"detail": "Missing or invalid bearer token"}, "AUTO_BROWSER_BEARER_TOKEN"),
            (400, {}, {"detail": "Missing required operator header: X-Operator-Id"}, "REQUIRE_OPERATOR_ID"),
            (429, {"retry-after": "12"}, {"detail": "Rate limit exceeded"}, "retry after 12s"),
        ]
        for status, headers, body, hint in cases:
            with self.subTest(status=status):
                client = RecordingHttpMcpClient(HttpMcpResponse(status_code=status, headers=headers, body=body))
                payload = _run_line(
                    StdioMcpBridge(client=client), json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
                )
                self.assertEqual(payload["id"], 9)
                self.assertEqual(payload["error"]["code"], -32000)
                self.assertIn(f"HTTP {status}", payload["error"]["message"])
                self.assertIn(body["detail"], payload["error"]["message"])
                self.assertIn(hint, payload["error"]["message"])

    def test_non_json_error_page_is_reported_with_its_status(self) -> None:
        self.assertIsNone(HttpMcpClient._decode_json(b"<html>502 Bad Gateway</html>"))
        client = RecordingHttpMcpClient(HttpMcpResponse(status_code=502, headers={}, body=None))
        payload = _run_line(
            StdioMcpBridge(client=client), json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        )
        self.assertEqual(payload["id"], 4)
        self.assertIn("502", payload["error"]["message"])

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
        error = HTTPError("http://ctrl.test/mcp", 401, "Unauthorized", headers, io.BytesIO(b'{"detail": "no"}'))

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
