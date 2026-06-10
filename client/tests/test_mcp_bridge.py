from __future__ import annotations

import io
import json
import unittest

from auto_browser_client.mcp_bridge import (
    MCP_PROTOCOL_HEADER,
    MCP_SESSION_HEADER,
    HttpMcpResponse,
    StdioMcpBridge,
    build_arg_parser,
)


class FakeHttpMcpClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []
        self.deleted_session_ids: list[str] = []

    def post_json(self, payload, *, session_id=None, protocol_version=None):
        self.posts.append(
            {
                "payload": payload,
                "session_id": session_id,
                "protocol_version": protocol_version,
            }
        )
        if payload.get("method") == "initialize":
            return HttpMcpResponse(
                status_code=200,
                headers={
                    MCP_SESSION_HEADER.lower(): "mcp-session-1",
                    MCP_PROTOCOL_HEADER.lower(): "2025-11-25",
                },
                body={
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}},
                },
            )
        return HttpMcpResponse(
            status_code=200,
            headers={},
            body={"jsonrpc": "2.0", "id": payload.get("id"), "result": {"ok": True}},
        )

    def delete_session(self, *, session_id=None):
        if session_id:
            self.deleted_session_ids.append(session_id)


class McpBridgeTests(unittest.TestCase):
    def test_bridge_adopts_session_and_protocol_from_initialize(self) -> None:
        client = FakeHttpMcpClient()
        bridge = StdioMcpBridge(client=client)
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            + "\n"
        )
        stdout = io.StringIO()

        exit_code = bridge.run(stdin=stdin, stdout=stdout)

        self.assertEqual(exit_code, 0)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(client.posts[1]["session_id"], "mcp-session-1")
        self.assertEqual(client.posts[1]["protocol_version"], "2025-11-25")
        self.assertEqual(client.deleted_session_ids, ["mcp-session-1"])

    def test_invalid_json_maps_to_parse_error(self) -> None:
        bridge = StdioMcpBridge(client=FakeHttpMcpClient())
        stdout = io.StringIO()

        bridge.run(stdin=io.StringIO("{not-json}\n"), stdout=stdout)

        payload = json.loads(stdout.getvalue().strip())
        self.assertEqual(payload["error"]["code"], -32700)

    def test_arg_parser_defaults_target_local_controller(self) -> None:
        args = build_arg_parser().parse_args([])
        self.assertEqual(args.base_url, "http://127.0.0.1:8000/mcp")
        self.assertEqual(args.timeout_seconds, 60.0)


if __name__ == "__main__":
    unittest.main()
