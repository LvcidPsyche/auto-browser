from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from app.mcp_transport import (
    MAX_MCP_SESSIONS,
    MAX_MCP_SESSIONS_PER_OWNER,
    MCP_PROTOCOL_HEADER,
    MCP_SESSION_HEADER,
    McpHttpTransport,
    McpSession,
)
from app.models import McpToolCallContent, McpToolCallResponse
from tests.test_mcp_resource_ownership import mcp_request


def _stream_request(session_id: str, protocol_version: str) -> Request:
    """A GET /mcp: the TestClient would buffer the endless stream."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/mcp",
            "query_string": b"",
            "headers": [
                (MCP_SESSION_HEADER.lower().encode(), session_id.encode()),
                (MCP_PROTOCOL_HEADER.lower().encode(), protocol_version.encode()),
            ],
        }
    )


class McpTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = SimpleNamespace(
            sessions={"session-1": object()},
            list_sessions=AsyncMock(return_value=[{"id": "session-1", "status": "active"}]),
            list_audit_events=AsyncMock(
                return_value=[
                    {
                        "event_type": "browser_action",
                        "session_id": "session-1",
                        "action": "click",
                    }
                ]
            ),
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
                    meta={"latency_ms": 3.5, "status": "ok", "tool": "browser.observe"},
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

    def _initialize_with_version(self, version: str):
        return self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": version,
                    "clientInfo": {"name": "pytest", "version": "1.0.0"},
                    "capabilities": {},
                },
            },
        )

    def test_unknown_protocol_version_gets_a_counter_offer_not_an_error(self) -> None:
        """The spec requires a supported version in the result, not an error.

        Returning -32602 killed the connection for any client opening with a
        version we do not speak — including a newer client probing for backwards
        compatibility, which is exactly what graceful downgrade exists for. The
        client compares the offer against what it supports and disconnects itself
        if it cannot proceed.
        """
        response = self._initialize_with_version("2099-01-01")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn("error", body, "unknown version must not be a hard error")
        self.assertEqual(body["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(response.headers[MCP_PROTOCOL_HEADER], "2025-11-25")

    def test_older_supported_protocol_version_is_honoured(self) -> None:
        """A counter-offer must not clobber a version we genuinely support."""
        response = self._initialize_with_version("2025-06-18")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["protocolVersion"], "2025-06-18")

    def test_2025_03_26_is_answered_with_a_counter_offer(self) -> None:
        """That version allowed JSON-RPC batches, which this transport refuses."""
        response = self._initialize_with_version("2025-03-26")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(response.headers[MCP_PROTOCOL_HEADER], "2025-11-25")

    def _restart_with_a_session_negotiated_at(self, version: str) -> str:
        """A session from before this version was dropped, restored from the store. Its id is returned."""
        store = Path(f"{self.tempdir.name}/mcp-sessions.json")
        session = McpSession(
            id="restored", protocol_version=version, client_info={}, client_capabilities={}, initialized=True
        )
        store.write_text(json.dumps([asdict(session)]), encoding="utf-8")
        self.transport = McpHttpTransport(
            tool_gateway=self.gateway,
            server_name="auto-browser",
            server_title="Auto Browser MCP",
            server_version="0.2.0",
            allowed_origins=["https://allowed.example"],
            session_store_path=str(store),
            manager=self.manager,
        )
        return session.id

    def test_a_restored_2025_03_26_session_keeps_working_without_the_version_header(self) -> None:
        """A client on 2025-03-26 sends no MCP-Protocol-Version header, so nothing changes until it initializes."""
        session_id = self._restart_with_a_session_negotiated_at("2025-03-26")

        response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["tools"][0]["name"], "browser.observe")
        self.assertEqual(response.headers[MCP_PROTOCOL_HEADER], "2025-03-26")

    def test_a_restored_2025_03_26_session_goes_on_at_its_own_version_with_the_header(self) -> None:
        """A stdio bridge sends its negotiated version on every POST and re-initializes only on a 404: refused with a
        400, a bridge that outlived the upgrade would never get past it."""
        session_id = self._restart_with_a_session_negotiated_at("2025-03-26")
        headers = {MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: "2025-03-26"}

        post = self.client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        stream = asyncio.run(self.transport.handle_get_request(_stream_request(session_id, "2025-03-26")))

        self.assertEqual(post.status_code, 200)
        self.assertEqual(post.json()["result"]["tools"][0]["name"], "browser.observe")
        self.assertEqual(post.headers[MCP_PROTOCOL_HEADER], "2025-03-26")
        self.assertIsInstance(stream, StreamingResponse)

    def test_a_2025_03_26_header_on_a_session_at_another_version_is_refused_as_unsupported(self) -> None:
        headers = {**self._ready_headers(), MCP_PROTOCOL_HEADER: "2025-03-26"}

        response = self.client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )

        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertEqual(error["code"], -32602)
        self.assertEqual(error["data"]["requested"], "2025-03-26")
        self.assertNotIn("2025-03-26", error["data"]["supported"])

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
        result = response.json()["result"]
        self.assertEqual(result["serverInfo"]["name"], "auto-browser")
        self.assertEqual(
            result["capabilities"]["experimental"]["autoBrowser"]["workflowProfiles"],
            ["fast", "governed"],
        )
        self.assertTrue(result["capabilities"]["experimental"]["autoBrowser"]["resumableAgentJobs"])
        self.assertTrue(result["capabilities"]["experimental"]["autoBrowser"]["discardableAgentJobs"])
        self.assertTrue(result["capabilities"]["experimental"]["autoBrowser"]["cancellableAgentJobs"])
        self.assertTrue(result["capabilities"]["resources"]["subscribe"])
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
        # -32005, not -32002: the latter is the spec's "resource not found" code
        # and is asserted as such elsewhere in this file. Sharing one code left
        # clients unable to distinguish "send initialized first" from "no such
        # resource".
        self.assertEqual(body["error"]["code"], -32005)
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
        self.assertEqual(call_response.json()["result"]["_meta"]["tool"], "browser.observe")
        self.gateway.call_tool.assert_awaited_once()
        called = self.gateway.call_tool.await_args.args[0]
        self.assertEqual(called.name, "browser.observe")
        self.assertEqual(called.arguments, {"session_id": "session-1"})

    def test_tools_call_omits_non_object_structured_content_for_2025_protocol(self) -> None:
        expected = [{"id": "session-1", "status": "active"}]
        self.gateway.call_tool.return_value = McpToolCallResponse(
            content=[McpToolCallContent(text=json.dumps(expected))],
            structuredContent=expected,
            isError=False,
        )
        session_id, protocol_version = self._initialize()
        self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )

        response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "browser.list_sessions", "arguments": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertNotIn("structuredContent", result)
        self.assertEqual(json.loads(result["content"][0]["text"]), expected)

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
        store_payload = json.loads(Path(f"{self.tempdir.name}/mcp-sessions.json").read_text(encoding="utf-8"))
        self.assertTrue(store_payload[0]["created_at"])
        self.assertEqual(restarted._sessions[session_id].created_at, store_payload[0]["created_at"])

    def test_persist_sessions_evicts_oldest_entries_after_limit(self) -> None:
        transport = McpHttpTransport(
            tool_gateway=self.gateway,
            server_name="auto-browser",
            server_title="Auto Browser MCP",
            server_version="0.2.0",
            allowed_origins=["https://allowed.example"],
            session_store_path=f"{self.tempdir.name}/mcp-sessions.json",
            manager=self.manager,
        )
        transport._sessions = {
            f"session-{index}": McpSession(
                id=f"session-{index}",
                protocol_version="2025-11-25",
                client_info={},
                client_capabilities={},
                initialized=bool(index % 2),
                created_at=f"2026-04-17T00:00:{index:02d}Z",
            )
            for index in range(503)
        }

        transport._persist_sessions()

        store_payload = json.loads(Path(f"{self.tempdir.name}/mcp-sessions.json").read_text(encoding="utf-8"))
        ids = [item["id"] for item in store_payload]
        self.assertEqual(len(store_payload), 500)
        self.assertNotIn("session-0", ids)
        self.assertNotIn("session-1", ids)
        self.assertNotIn("session-2", ids)
        self.assertIn("session-3", ids)

    def test_eviction_keeps_a_long_lived_session_that_is_still_in_use(self) -> None:
        transport = McpHttpTransport(
            tool_gateway=self.gateway,
            server_name="auto-browser",
            server_title="Auto Browser MCP",
            server_version="0.2.0",
            allowed_origins=["https://allowed.example"],
            session_store_path=None,
            manager=self.manager,
        )
        transport._sessions = {
            f"session-{index}": McpSession(
                id=f"session-{index}",
                protocol_version="2025-11-25",
                client_info={},
                client_capabilities={},
                created_at=f"2026-04-17T00:{index // 60:02d}:{index % 60:02d}Z",
            )
            for index in range(500)
        }
        transport._sessions["session-0"].last_used_at = "2026-04-18T00:00:00Z"
        transport._sessions["new"] = McpSession(
            id="new", protocol_version="2025-11-25", client_info={}, client_capabilities={}
        )
        transport._evict_stale_sessions()

        self.assertEqual(len(transport._sessions), 500)
        self.assertIn("session-0", transport._sessions)
        self.assertNotIn("session-1", transport._sessions)

    def test_requests_refresh_a_sessions_last_use(self) -> None:
        session_id, protocol_version = self._initialize()
        self.transport._sessions[session_id].last_used_at = "2000-01-01T00:00:00Z"

        self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        self.assertGreater(self.transport._sessions[session_id].last_used_at, "2000-01-01T00:00:00Z")
        store_payload = json.loads(Path(f"{self.tempdir.name}/mcp-sessions.json").read_text(encoding="utf-8"))
        self.assertEqual(store_payload[0]["last_used_at"], self.transport._sessions[session_id].last_used_at)

    def test_an_unsupported_protocol_version_header_is_refused_400(self) -> None:
        headers = {**self._ready_headers(), MCP_PROTOCOL_HEADER: "2099-01-01"}

        response = self.client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 30, "method": "ping"})

        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertEqual(error["code"], -32602)
        self.assertEqual(error["data"]["requested"], "2099-01-01")
        self.assertIn("2025-11-25", error["data"]["supported"])

    def test_a_protocol_version_header_that_differs_from_the_sessions_is_refused_400(self) -> None:
        headers = {**self._ready_headers(), MCP_PROTOCOL_HEADER: "2025-06-18"}

        response = self.client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 31, "method": "ping"})

        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertEqual(error["code"], -32600)
        self.assertIn("Protocol version mismatch", error["message"])

    def test_a_failing_tool_is_isError_true_on_the_wire(self) -> None:
        self.gateway.call_tool.return_value = McpToolCallResponse(
            content=[McpToolCallContent(text="the tool failed")], isError=True
        )
        headers = self._ready_headers()

        response = self.client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 32,
                "method": "tools/call",
                "params": {"name": "browser.observe", "arguments": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn("error", body)
        self.assertIs(body["result"]["isError"], True)
        self.assertEqual(body["result"]["content"][0]["text"], "the tool failed")

    def test_ping_answers_an_empty_result(self) -> None:
        """Before notifications/initialized too: ping is the one method allowed that early."""
        session_id, protocol_version = self._initialize()

        response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "id": 33, "method": "ping"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"jsonrpc": "2.0", "id": 33, "result": {}})

    def test_params_that_are_invalid_are_refused(self) -> None:
        headers = self._ready_headers()
        calls = (
            ({"jsonrpc": "2.0", "id": 34, "method": "ping", "params": ["not", "an", "object"]}, "object"),
            ({"jsonrpc": "2.0", "id": 35, "method": "tools/call", "params": {"arguments": {}}}, "Invalid tools/call"),
        )
        for payload, message in calls:
            with self.subTest(message=message):
                response = self.client.post("/mcp", headers=headers, json=payload)

                self.assertEqual(response.status_code, 200)
                error = response.json()["error"]
                self.assertEqual(error["code"], -32602)
                self.assertIn(message, error["message"])

    def test_a_batch_is_refused_400(self) -> None:
        response = self.client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 36, "method": "ping"}])

        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertEqual(error["code"], -32600)
        self.assertIn("batches", error["message"])

    def test_get_without_a_session_header_is_refused_400(self) -> None:
        response = self.client.get("/mcp")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], -32000)

    def test_get_for_an_unknown_session_is_refused_404(self) -> None:
        response = self.client.get("/mcp", headers={MCP_SESSION_HEADER: "no-such-session"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], -32001)

    def test_get_before_notifications_initialized_is_refused(self) -> None:
        session_id, protocol_version = self._initialize()

        response = self.client.get(
            "/mcp", headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version}
        )

        self.assertEqual(response.status_code, 200)
        error = response.json()["error"]
        self.assertEqual(error["code"], -32005)
        self.assertIn("notifications/initialized", error["message"])

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

    def test_resources_read_refuses_a_uri_that_is_not_a_non_empty_string(self) -> None:
        session_id, protocol_version = self._initialize()
        headers = {MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version}
        self.client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )

        for params in ({"uri": 5}, {"uri": []}, {"uri": {}}, {"uri": None}, {"uri": ""}, {}):
            with self.subTest(params=params):
                response = self.client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 16, "method": "resources/read", "params": params},
                )

                self.assertEqual(response.status_code, 200)
                error = response.json()["error"]
                self.assertEqual(error["code"], -32602)
                self.assertEqual(error["message"], "resources/read requires a non-empty uri")

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

    def test_resources_list_and_read_audit_events_work_after_initialization(self) -> None:
        session_id, protocol_version = self._initialize()
        self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )

        list_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "id": 13, "method": "resources/list", "params": {}},
        )
        self.assertEqual(list_response.status_code, 200)
        resource_uris = {item["uri"] for item in list_response.json()["result"]["resources"]}
        self.assertIn("browser://audit/events", resource_uris)

        read_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={
                "jsonrpc": "2.0",
                "id": 14,
                "method": "resources/read",
                "params": {"uri": "browser://audit/events"},
            },
        )

        self.assertEqual(read_response.status_code, 200)
        contents = read_response.json()["result"]["contents"]
        self.assertEqual(contents[0]["uri"], "browser://audit/events")
        self.assertEqual(contents[0]["mimeType"], "application/json")
        self.assertEqual(json.loads(contents[0]["text"])[0]["event_type"], "browser_action")
        self.manager.list_audit_events.assert_awaited_once_with(limit=100)

    def test_resources_subscribe_and_unsubscribe_track_session_state(self) -> None:
        session_id, protocol_version = self._initialize()
        self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )

        subscribe_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={
                "jsonrpc": "2.0",
                "id": 13,
                "method": "resources/subscribe",
                "params": {"uri": "browser://sessions"},
            },
        )

        self.assertEqual(subscribe_response.status_code, 200)
        self.assertEqual(subscribe_response.json()["result"], {})
        self.assertEqual(self.transport._sessions[session_id].resource_subscriptions, ["browser://sessions"])

        unsubscribe_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={
                "jsonrpc": "2.0",
                "id": 14,
                "method": "resources/unsubscribe",
                "params": {"uri": "browser://sessions"},
            },
        )

        self.assertEqual(unsubscribe_response.status_code, 200)
        self.assertEqual(unsubscribe_response.json()["result"], {})
        self.assertEqual(self.transport._sessions[session_id].resource_subscriptions, [])

    def test_resources_subscribe_rejects_unknown_uri(self) -> None:
        session_id, protocol_version = self._initialize()
        self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )

        subscribe_response = self.client.post(
            "/mcp",
            headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version},
            json={
                "jsonrpc": "2.0",
                "id": 15,
                "method": "resources/subscribe",
                "params": {"uri": "browser://session-1/missing"},
            },
        )

        self.assertEqual(subscribe_response.status_code, 200)
        self.assertEqual(subscribe_response.json()["error"]["code"], -32002)

    def _ready_headers(self) -> dict[str, str]:
        """The headers of a session that has sent notifications/initialized."""
        session_id, protocol_version = self._initialize()
        headers = {MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: protocol_version}
        self.client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )
        return headers

    def _resource_call(self, headers: dict[str, str], method: str, uri: str):
        return self.client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 17, "method": method, "params": {"uri": uri}}
        )

    def test_subscribing_to_the_audit_events_is_refused_because_nothing_notifies_them(self) -> None:
        headers = self._ready_headers()

        response = self._resource_call(headers, "resources/subscribe", "browser://audit/events")

        self.assertEqual(response.status_code, 200)
        error = response.json()["error"]
        self.assertEqual(error["code"], -32602)
        self.assertIn("poll resources/read", error["message"])
        self.assertEqual(self.transport._sessions[headers[MCP_SESSION_HEADER]].resource_subscriptions, [])

    def test_a_manager_without_audit_events_answers_the_subscribe_as_an_unknown_resource(self) -> None:
        headers = self._ready_headers()
        self.manager.list_audit_events = None

        response = self._resource_call(headers, "resources/subscribe", "browser://audit/events")

        self.assertEqual(response.json()["error"]["code"], -32002)

    def test_a_persisted_subscription_to_the_audit_events_stays_silent_and_can_be_removed(self) -> None:
        """A version that accepted the subscribe left this in the store."""
        headers = self._ready_headers()
        session = self.transport._sessions[headers[MCP_SESSION_HEADER]]
        session.resource_subscriptions.append("browser://audit/events")

        for event in ("observe", "action", "session", "approval"):
            uris = McpHttpTransport._updated_resource_uris(
                {"event": event, "session_id": "session-1"}, subscribed=session.resource_subscriptions
            )
            self.assertEqual(uris, [], event)
        response = self._resource_call(headers, "resources/unsubscribe", "browser://audit/events")

        self.assertEqual(response.json()["result"], {})
        self.assertEqual(session.resource_subscriptions, [])

    def test_resource_update_mapping_only_emits_subscribed_resources(self) -> None:
        uris = McpHttpTransport._updated_resource_uris(
            {"event": "observe", "session_id": "session-1"},
            subscribed=[
                "browser://sessions",
                "browser://session-1/dom",
                "browser://session-1/network",
            ],
        )

        self.assertEqual(uris, ["browser://session-1/dom", "browser://sessions"])


def _owned_sessions(owner: str | None, count: int, *, minute: int) -> dict[str, McpSession]:
    """count sessions of one owner, used in turn during one minute (later index, later use)."""
    return {
        f"{owner}-{index}": McpSession(
            id=f"{owner}-{index}",
            protocol_version="2025-11-25",
            client_info={},
            client_capabilities={},
            created_at=f"2026-04-17T00:{minute:02d}:00.{index:06d}Z",
            owner=owner,
        )
        for index in range(count)
    }


class McpSessionCapTests(unittest.TestCase):
    """The table's cap ignored owners: one operator's initialize flood evicted another's live session."""

    def setUp(self) -> None:
        self.transport = McpHttpTransport(
            tool_gateway=SimpleNamespace(), server_name="auto-browser", server_version="0.2.0", manager=None
        )

    def test_one_operators_flood_does_not_evict_another_operators_session(self) -> None:
        self.transport._sessions = {**_owned_sessions("alice", 1, minute=0), **_owned_sessions("bob", 600, minute=1)}

        self.transport._evict_stale_sessions()

        self.assertIn("alice-0", self.transport._sessions)

    def test_an_owner_over_its_cap_loses_its_own_least_recently_used_sessions(self) -> None:
        self.transport._sessions = {
            **_owned_sessions("alice", 1, minute=0),
            **_owned_sessions("bob", MAX_MCP_SESSIONS_PER_OWNER + 2, minute=1),
        }

        self.transport._evict_stale_sessions()

        bob = sorted((key for key in self.transport._sessions if key.startswith("bob-")), key=lambda k: int(k[4:]))
        self.assertEqual(len(bob), MAX_MCP_SESSIONS_PER_OWNER)
        self.assertEqual(bob[0], "bob-2")
        self.assertIn("alice-0", self.transport._sessions)

    def test_control_the_global_cap_still_bounds_the_table(self) -> None:
        owners = [f"operator-{n}" for n in range(MAX_MCP_SESSIONS // MAX_MCP_SESSIONS_PER_OWNER + 1)]
        for minute, owner in enumerate(owners):
            self.transport._sessions.update(_owned_sessions(owner, MAX_MCP_SESSIONS_PER_OWNER, minute=minute))
        self.transport._sessions.update(_owned_sessions(None, 10, minute=59))

        self.transport._evict_stale_sessions()

        self.assertEqual(len(self.transport._sessions), MAX_MCP_SESSIONS)
        self.assertNotIn("operator-0-0", self.transport._sessions)
        self.assertIn("None-9", self.transport._sessions)


class McpGetStreamTests(unittest.IsolatedAsyncioTestCase):
    """The TestClient buffers the whole response, so the success case is driven here and read under a timeout."""

    async def test_get_opens_an_event_stream_only_for_an_initialized_session(self) -> None:
        transport = McpHttpTransport(
            tool_gateway=SimpleNamespace(list_tools=lambda: []),
            server_name="auto-browser",
            server_version="0.2.0",
            manager=None,
        )
        session = McpSession(id="m1", protocol_version="2025-11-25", client_info={}, client_capabilities={})
        transport._sessions["m1"] = session

        refused = await transport.handle_get_request(mcp_request("GET", "m1"))

        self.assertNotIsInstance(refused, StreamingResponse)
        self.assertEqual(json.loads(refused.body)["error"]["code"], -32005)

        session.initialized = True
        response = await transport.handle_get_request(mcp_request("GET", "m1"))

        self.assertIsInstance(response, StreamingResponse)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(response.headers[MCP_PROTOCOL_HEADER], "2025-11-25")
        self.assertEqual(await asyncio.wait_for(response.body_iterator.__anext__(), 5), ": connected\n\n")
        await response.body_iterator.aclose()


class McpResourceEncodingTests(unittest.IsolatedAsyncioTestCase):
    """A resource read is encoded as strictly as a tool result: strict JSON, and never an object's repr."""

    URIS = ("browser://sessions", "browser://audit/events", "browser://s1/console", "browser://s1/network")

    def _transport(self, value: object) -> McpHttpTransport:
        manager = SimpleNamespace(
            list_sessions=AsyncMock(return_value=[{"id": "s1", "score": value}]),
            list_audit_events=AsyncMock(return_value=[{"event_type": "x", "n": value}]),
            get_console_messages=AsyncMock(return_value={"items": [{"text": "t", "at": value}]}),
            get_network_log=AsyncMock(return_value={"entries": [{"url": "u", "timing": value}]}),
        )
        return McpHttpTransport(
            tool_gateway=SimpleNamespace(list_tools=lambda: []),
            server_name="auto-browser",
            server_version="0",
            manager=manager,
        )

    async def test_a_nan_or_an_infinity_in_a_resource_is_sent_as_null(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            transport = self._transport(value)
            for uri in self.URIS:
                with self.subTest(uri=uri, value=value):
                    resource = await transport._read_resource(uri)

                    parsed = json.loads(resource["text"], parse_constant=lambda name: self.fail(f"{uri} sent {name}"))
                    self.assertIn(None, [*parsed[0].values()])

    async def test_a_resource_that_cannot_be_encoded_is_not_served(self) -> None:
        transport = self._transport(object())
        for uri in self.URIS:
            with self.subTest(uri=uri), self.assertRaises(TypeError):
                await transport._read_resource(uri)


if __name__ == "__main__":
    unittest.main()
