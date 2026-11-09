"""A lone surrogate from a page no longer fails a response.

A page can hand over a lone surrogate (JSON's "\\ud83d" escape decodes to one), and UTF-8 cannot carry it: the response
raised UnicodeEncodeError after the handler had run, so tools/call and any REST route returning page text answered 500.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app.main as main_module
from app.action_errors import BrowserActionError, SessionNotFoundError
from app.main import handle_browser_action_error, handle_key_not_found
from app.mcp_transport import McpHttpTransport, McpSession
from app.models import McpToolCallContent, McpToolCallResponse
from app.safe_json_response import SafeJSONResponse
from tests.test_mcp_resource_ownership import PROTOCOL, mcp_request

# A lone surrogate, as a page's broken text can carry: UTF-8 cannot encode it.
LONE = "\ud83d"
TEXT = f"a{LONE}b"
REPLACED = "a\ufffdb"


class SafeJSONResponseTests(unittest.TestCase):
    def test_a_normal_body_is_exactly_what_json_response_writes(self) -> None:
        content = {"text": "café \U0001f600", "n": 1, "nested": ["x", {"y": None}]}

        self.assertEqual(SafeJSONResponse(content).body, JSONResponse(content).body)

    def test_a_lone_surrogate_is_replaced_in_values_keys_and_nesting(self) -> None:
        body = SafeJSONResponse({"text": TEXT, f"key{LONE}": [TEXT, {"deep": TEXT}]}).body

        self.assertEqual(json.loads(body), {"text": REPLACED, "key\ufffd": [REPLACED, {"deep": REPLACED}]})

    def test_the_replacement_is_the_character_not_the_json_escape(self) -> None:
        body = SafeJSONResponse({"text": TEXT}).body

        self.assertIn("\ufffd".encode(), body)
        self.assertNotIn(b"\\ud83d", body)


class McpLoneSurrogateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.gateway = SimpleNamespace(list_tools=lambda: [], call_tool=AsyncMock())
        self.page = SimpleNamespace(content=AsyncMock(return_value=f"<p>{TEXT}</p>"))
        self.manager = SimpleNamespace(
            sessions={},
            get_session=AsyncMock(return_value=SimpleNamespace(page=self.page, lock=None)),
        )
        self.transport = McpHttpTransport(
            tool_gateway=self.gateway,
            server_name="auto-browser",
            server_version="0",
            manager=self.manager,
        )
        self.transport._sessions["m1"] = McpSession(
            id="m1", protocol_version=PROTOCOL, client_info={}, client_capabilities={}, initialized=True
        )

    async def post(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": 7, "method": method, "params": params}
        response = await self.transport.handle_post_request(mcp_request("POST", "m1", body))
        self.assertEqual(response.status_code, 200)
        return json.loads(response.body)

    async def test_a_tool_result_holding_one_answers_200_with_the_replacement_character(self) -> None:
        self.gateway.call_tool.return_value = McpToolCallResponse(
            content=[McpToolCallContent(text=json.dumps({"title": TEXT}, ensure_ascii=False))],
            structuredContent={"title": TEXT},
        )

        answer = await self.post("tools/call", {"name": "browser.observe", "arguments": {}})

        self.assertEqual(answer["result"]["structuredContent"]["title"], REPLACED)
        self.assertEqual(json.loads(answer["result"]["content"][0]["text"]), {"title": REPLACED})

    async def test_a_uri_holding_one_answers_not_found_and_not_a_500(self) -> None:
        answer = await self.post("resources/read", {"uri": f"browser://s1/{TEXT}"})

        self.assertEqual(answer["error"]["code"], -32002)
        self.assertEqual(answer["error"]["message"], f"Resource not found: browser://s1/{REPLACED}")

    async def test_a_browser_action_error_holding_one_is_answered_with_the_replacement_character(self) -> None:
        response = await handle_browser_action_error(None, BrowserActionError(TEXT, code="x", details={"k": TEXT}))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"], REPLACED)
        self.assertEqual(json.loads(response.body)["k"], REPLACED)

    async def test_a_key_error_naming_one_is_answered_with_the_replacement_character(self) -> None:
        session_not_found = SessionNotFoundError(f"s-{LONE}")
        request = SimpleNamespace(method="GET", url=SimpleNamespace(path="/x"))

        known = await handle_key_not_found(request, session_not_found)
        with self.assertLogs("app.main", "WARNING"):
            plain = await handle_key_not_found(request, KeyError(f"s-{LONE}"))

        self.assertEqual(known.status_code, 404)
        self.assertEqual(plain.status_code, 404)
        self.assertIn("\ufffd", json.loads(known.body)["detail"])
        self.assertIn("\ufffd", json.loads(plain.body)["detail"])


class RestRouteLoneSurrogateTests(unittest.TestCase):
    """Through the real app: a route goes out by the app's default class, with a response model or without one."""

    def setUp(self) -> None:
        patcher = patch.object(main_module.manager, "ensure_session_accessible", new=AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main_module.app, base_url="http://127.0.0.1", raise_server_exceptions=False)

    def test_a_route_with_a_response_model_returns_a_pages_string_with_the_replacement_character(self) -> None:
        page = {"url": "https://x.test", "title": TEXT, "text_excerpt": TEXT}

        with patch.object(main_module.manager, "observe", new=AsyncMock(return_value=page)):
            response = self.client.get("/sessions/s1/observe")

        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(response.json()["title"], REPLACED)

    def test_a_route_without_a_response_model_returns_a_pages_string_with_the_replacement_character(self) -> None:
        inspector = SimpleNamespace(entries=lambda **_: [{"url": f"https://x.test/{TEXT}"}], summary=lambda: {})

        with patch("app.routes.extensions.network._get_inspector", return_value=inspector):
            response = self.client.get("/sessions/s1/network/requests")

        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(response.json()["requests"][0]["url"], f"https://x.test/{REPLACED}")


if __name__ == "__main__":
    unittest.main()
