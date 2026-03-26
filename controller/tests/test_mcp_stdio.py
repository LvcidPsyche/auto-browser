from __future__ import annotations

import io
import json
import unittest

from app.mcp_stdio import HttpMcpResponse, StdioMcpBridge
from app.mcp_transport import MCP_PROTOCOL_HEADER, MCP_SESSION_HEADER


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
        method = payload.get("method")
        if method == "initialize":
            return HttpMcpResponse(
                status_code=200,
                headers={
                    MCP_SESSION_HEADER.lower(): "mcp-session-1",
                    MCP_PROTOCOL_HEADER.lower(): "2025-11-25",
                },
                body={
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "auto-browser", "version": "0.2.0"},
                    },
                },
            )
        if method == "notifications/initialized":
            return HttpMcpResponse(status_code=202, headers={}, body=None)
        return HttpMcpResponse(
            status_code=200,
            headers={},
            body={
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"ok": True},
            },
        )

    def delete_session(self, *, session_id=None):
        if session_id:
            self.deleted_session_ids.append(session_id)


class McpStdioBridgeTests(unittest.TestCase):
    def test_bridge_tracks_session_and_protocol_headers(self) -> None:
        client = FakeHttpMcpClient()
        bridge = StdioMcpBridge(client=client)

        stdin = io.StringIO(
            "\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-11-25",
                                "clientInfo": {"name": "pytest", "version": "1.0.0"},
                                "capabilities": {},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/initialized",
                            "params": {},
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/list",
                            "params": {},
                        }
                    ),
                ]
            )
            + "\n"
        )
        stdout = io.StringIO()

        exit_code = bridge.run(stdin=stdin, stdout=stdout)

        self.assertEqual(exit_code, 0)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(lines[1]["result"]["ok"], True)
        self.assertEqual(client.posts[0]["session_id"], None)
        self.assertEqual(client.posts[1]["session_id"], "mcp-session-1")
        self.assertEqual(client.posts[2]["protocol_version"], "2025-11-25")
        self.assertEqual(client.deleted_session_ids, ["mcp-session-1"])

    def test_invalid_json_returns_parse_error(self) -> None:
        bridge = StdioMcpBridge(client=FakeHttpMcpClient())
        stdout = io.StringIO()

        bridge.run(stdin=io.StringIO("{not-json}\n"), stdout=stdout)

        payload = json.loads(stdout.getvalue().strip())
        self.assertEqual(payload["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
