"""A proxy persona's password is write-only through the API.

GET /proxy-personas/{name} returned the stored persona, plaintext password
included, to any caller holding an API token, while the list endpoint already
answered `has_password`. Only session creation needs the real password, and it
gets it from resolve_proxy.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.browser_manager import BrowserManager
from app.models import McpToolCallRequest
from app.proxy_personas import ProxyPersonaStore
from app.routes.operations import create_operations_router
from app.tool_gateway.gateway import McpToolGateway
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage, _settings

PASSWORD = "hunter2-proxy-password"


class ProxyPersonaPasswordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "proxy-personas.json"
        self.store = ProxyPersonaStore(self.path)
        self.store.set_persona(
            "us-east",
            server="http://proxy.example.com:8080",
            username="alice",
            password=PASSWORD,
            description="primary",
        )
        app = FastAPI()
        app.include_router(
            create_operations_router(manager=MagicMock(), proxy_store=self.store, cron_service=MagicMock())
        )
        self.client = TestClient(app)
        self.gateway = McpToolGateway(
            manager=MagicMock(),
            orchestrator=MagicMock(),
            job_queue=MagicMock(),
            proxy_store=self.store,
            tool_profile="full",
        )

    async def asyncTearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    # -- the API never returns the password ---------------------------------

    def test_get_persona_over_http_leaves_out_the_password(self) -> None:
        response = self.client.get("/proxy-personas/us-east")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(PASSWORD, response.text)
        self.assertNotIn("password", response.json())
        self.assertEqual(
            response.json(),
            {
                "name": "us-east",
                "server": "http://proxy.example.com:8080",
                "username": "alice",
                "has_password": True,
                "description": "primary",
            },
        )

    def test_get_persona_without_a_password_says_so(self) -> None:
        self.store.set_persona("open", server="http://open.example.com:3128")

        body = self.client.get("/proxy-personas/open").json()

        self.assertIs(body["has_password"], False)
        self.assertNotIn("password", body)

    def test_get_of_an_unknown_persona_is_still_404(self) -> None:
        self.assertEqual(self.client.get("/proxy-personas/missing").status_code, 404)

    def test_list_and_create_over_http_do_not_echo_the_password(self) -> None:
        listed = self.client.get("/proxy-personas")
        created = self.client.post(
            "/proxy-personas",
            json={"name": "eu", "server": "http://eu.example.com:8080", "username": "bob", "password": PASSWORD},
        )

        self.assertNotIn(PASSWORD, listed.text)
        self.assertEqual(created.status_code, 200)
        self.assertNotIn(PASSWORD, created.text)
        self.assertIs(created.json()["has_password"], True)

    async def test_mcp_tools_do_not_return_the_password(self) -> None:
        listed = await self.gateway.call_tool(McpToolCallRequest(name="browser.list_proxy_personas", arguments={}))
        created = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.create_proxy_persona",
                arguments={"name": "eu", "server": "http://eu.example.com:8080", "password": PASSWORD},
            )
        )

        for response in (listed, created):
            self.assertFalse(response.isError)
            self.assertNotIn(PASSWORD, response.model_dump_json())
        self.assertIs(created.structuredContent["has_password"], True)

    def test_the_store_does_not_hand_out_the_password_through_get_persona(self) -> None:
        self.assertNotIn("password", self.store.get_persona("us-east"))

    # -- what must keep working ---------------------------------------------

    def test_the_password_is_still_stored_on_disk(self) -> None:
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["us-east"]["password"], PASSWORD)

    def test_resolve_proxy_still_returns_the_real_password(self) -> None:
        self.assertEqual(
            self.store.resolve_proxy("us-east"),
            {"server": "http://proxy.example.com:8080", "username": "alice", "password": PASSWORD},
        )

    def test_resolve_proxy_of_an_unknown_persona_still_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.store.resolve_proxy("missing")

    async def test_a_session_with_a_persona_still_gets_the_real_password_in_its_browser_context(self) -> None:
        manager = BrowserManager(_settings(self.root), proxy_store=self.store)
        manager._settle = AsyncMock()  # type: ignore[method-assign]
        manager._maybe_provision_session_tunnel = AsyncMock()  # type: ignore[method-assign]
        browser = FakeBrowser(FakeContext(FakePage()))
        manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]

        await manager.create_session(name="via-persona", proxy_persona="us-east")

        proxy = browser.new_context.await_args.kwargs["proxy"]
        self.assertEqual(proxy["server"], "http://proxy.example.com:8080")
        self.assertEqual(proxy["username"], "alice")
        self.assertEqual(proxy["password"], PASSWORD)


if __name__ == "__main__":
    unittest.main()
