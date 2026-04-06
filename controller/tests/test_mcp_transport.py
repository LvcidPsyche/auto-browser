from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.mcp_transport import MCP_PROTOCOL_HEADER, MCP_SESSION_HEADER, McpHttpTransport
from app.models import McpToolCallContent, McpToolCallResponse


class McpTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = SimpleNamespace(
            sessions={"session-1": object()},
            list_sessions=AsyncMock(return_value=[{"id": "session-1", "status": "active"}]),
        )
        self.gateway = SimpleNamespace(
            list_tools=lambda: [
                {
                    "name": "browser.observe",
                    "description": "Observe one session.",
                    "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}},
                }
            ],
            call_tool=AsyncMock(
                return_value=McpToolCallResponse(
                    content=[McpToolCallContent(text='{"session_id":"session-1"}')],
                    structuredContent={"session_id": "session-1", "status": "ok"},
                    isError=False,
                )
            ),
        )
        self.transport = McpHttpTransport(
            tool_gateway=self.gateway,
            server_name="auto-browser",
            server_title="Auto Browser MCP",
            server_version="0.2.0",
            allowed_origins=["https://allowed.example"],
            session_store_path=f"{self.tempdir.name}/mcp-sessions.json",
            manager=self.manager,
        )
        app = FastAPI()

        @app.get("/mcp")
        async def get_mcp(request: Request):
            return await self.transport.handle_get_request(request)

        @app.post("/mcp")
        async def post_mcp(request: Request):
            return await self.transport.handle_post_request(request)

        @app.delete("/mcp")
        async def delete_mcp(request: Request):
            return await self.transport.handle_delete_request(request)

        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _initialize(self) -> tuple[str, str]:
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "clientInfo": {"name": "pytest", "version": "1.0.0"},
                    "capabilities": {"roots": {}},
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        session_id = response.headers[MCP_SESSION_HEADER]
        protocol_version = response.headers[MCP_PROTOCOL_HEADER]
        self.assertEqual(protocol_version, "2025-11-25")
        self.assertEqual(response.json()["result"]["serverInfo"]["name"], "auto-browser")
        return session_id, protocol_version

    def test_initialize_requires_initialized_notification_before_tool_calls(self) -> None:
        session_id, protocol_version = self._initialize()

        response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["error"]["code"], -32002)
        self.assertIn("notifications/initialized", body["error"]["message"])

    def test_tools_list_and_call_work_after_initialization(self) -> None:
        session_id, protocol_version = self._initialize()

        init_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
        self.assertEqual(init_response.status_code, 202)

        list_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["result"]["tools"][0]["name"], "browser.observe")

        call_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "browser.observe", "arguments": {"session_id": "session-1"}},
            },
        )
        self.assertEqual(call_response.status_code, 200)
        self.assertEqual(call_response.json()["result"]["structuredContent"]["session_id"], "session-1")
        self.gateway.call_tool.assert_awaited_once()
        called = self.gateway.call_tool.await_args.args[0]
        self.assertEqual(called.name, "browser.observe")
        self.assertEqual(called.arguments, {"session_id": "session-1"})

    def test_delete_tears_down_session(self) -> None:
        session_id, protocol_version = self._initialize()
        self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )

        delete_response = self.client.delete("/mcp", headers={MCP_SESSION_HEADER: session_id})
        self.assertEqual(delete_response.status_code, 204)

        missing_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
        )
        self.assertEqual(missing_response.status_code, 404)
        self.assertEqual(missing_response.json()["error"]["code"], -32001)

    def test_origin_allowlist_blocks_untrusted_browser_origins(self) -> None:
        response = self.client.post(
            "/mcp",
            headers={"Origin": "https://evil.example"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "clientInfo": {}, "capabilities": {}},
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], -32000)

    def test_sessions_survive_transport_restart_when_store_path_is_configured(self) -> None:
        session_id, protocol_version = self._initialize()
        init_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
        self.assertEqual(init_response.status_code, 202)

        restarted = McpHttpTransport(
            tool_gateway=self.gateway,
            server_name="auto-browser",
            server_title="Auto Browser MCP",
            server_version="0.2.0",
            allowed_origins=["https://allowed.example"],
            session_store_path=f"{self.tempdir.name}/mcp-sessions.json",
            manager=self.manager,
        )
        app = FastAPI()

        @app.post("/mcp")
        async def post_mcp(request: Request):
            return await restarted.handle_post_request(request)

        restarted_client = TestClient(app)
        list_response = restarted_client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}},
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["result"]["tools"][0]["name"], "browser.observe")

    def test_missing_method_returns_invalid_request_error(self) -> None:
        response = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 9, "params": {}},
        )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"]["code"], -32600)
        self.assertEqual(body["error"]["message"], "JSON-RPC method is required")

    def test_resources_list_and_read_sessions_work_after_initialization(self) -> None:
        session_id, protocol_version = self._initialize()
        init_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
        self.assertEqual(init_response.status_code, 202)

        list_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "id": 10, "method": "resources/list", "params": {}},
        )
        self.assertEqual(list_response.status_code, 200)
        resource_uris = {item["uri"] for item in list_response.json()["result"]["resources"]}
        self.assertIn("browser://sessions", resource_uris)
        self.assertIn("browser://session-1/console", resource_uris)

        read_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={
                "jsonrpc": "2.0",
                "id": 11,
                "method": "resources/read",
                "params": {"uri": "browser://sessions"},
            },
        )
        self.assertEqual(read_response.status_code, 200)
        contents = read_response.json()["result"]["contents"]
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0]["uri"], "browser://sessions")
        self.assertEqual(contents[0]["mimeType"], "application/json")
        self.assertEqual(contents[0]["text"], '[{"id": "session-1", "status": "active"}]')
        self.manager.list_sessions.assert_awaited_once_with()

    def test_resources_read_returns_not_found_error_for_unknown_uri(self) -> None:
        session_id, protocol_version = self._initialize()
        self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )

        read_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={
                "jsonrpc": "2.0",
                "id": 12,
                "method": "resources/read",
                "params": {"uri": "browser://session-1/does-not-exist"},
            },
        )

        self.assertEqual(read_response.status_code, 200)
        body = read_response.json()
        self.assertEqual(body["error"]["code"], -32002)
        self.assertIn("Resource not found", body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
