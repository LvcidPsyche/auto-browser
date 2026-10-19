from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

import httpx
from auto_browser_client import AutoBrowserClient


def _json_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


class ClientPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[httpx.Request] = []
        self.client = AutoBrowserClient("http://auto-browser.test")

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json={"ok": True})

        self.client._sync_client = httpx.Client(
            base_url=self.client.base_url,
            transport=httpx.MockTransport(handler),
        )

    def tearDown(self) -> None:
        if self.client._sync_client is not None:
            self.client._sync_client.close()

    def test_actions_use_server_action_routes(self) -> None:
        self.client.navigate("session-1", "https://example.com")
        self.client.click("session-1", selector="#submit")
        self.client.type_text("session-1", "hello", selector="#search", clear_first=False)
        self.client.scroll("session-1", delta_x=2, delta_y=300)

        self.assertEqual(
            [request.url.path for request in self.requests],
            [
                "/sessions/session-1/actions/navigate",
                "/sessions/session-1/actions/click",
                "/sessions/session-1/actions/type",
                "/sessions/session-1/actions/scroll",
            ],
        )
        self.assertEqual(_json_body(self.requests[0]), {"url": "https://example.com"})
        self.assertEqual(_json_body(self.requests[1]), {"selector": "#submit"})
        self.assertEqual(
            _json_body(self.requests[2]),
            {"text": "hello", "clear_first": False, "selector": "#search"},
        )
        self.assertEqual(_json_body(self.requests[3]), {"delta_x": 2, "delta_y": 300})

    def test_audit_events_use_server_route(self) -> None:
        self.client.list_audit_events(limit=10, session_id="session-1")

        request = self.requests[-1]
        self.assertEqual(request.url.path, "/audit/events")
        self.assertEqual(dict(request.url.params), {"limit": "10", "session_id": "session-1"})

    def test_ids_cannot_retarget_the_request(self) -> None:
        # An id is one path segment. "../approvals/zz/approve" used to reach
        # POST /approvals/zz/approve through get_session's own route.
        hostile = "../approvals/zz/approve?x=1"
        self.client.get_session(hostile)
        self.client.navigate("a/b", "https://example.com")
        self.client.approve("x#y", "ok")

        self.assertEqual(
            [request.url.raw_path.decode() for request in self.requests],
            [
                "/sessions/..%2Fapprovals%2Fzz%2Fapprove%3Fx%3D1",
                "/sessions/a%2Fb/actions/navigate",
                "/approvals/x%23y/approve",
            ],
        )

    def test_dot_segment_ids_are_refused(self) -> None:
        # "." and ".." survive percent-encoding and are resolved by URL normalisation.
        for bad in ("..", ".", ""):
            with self.assertRaises(ValueError):
                self.client.get_session(bad)
        self.assertEqual(self.requests, [])

    def test_stream_events_url_encodes_the_session_id(self) -> None:
        captured: dict[str, Any] = {}

        def fake_stream(method: str, url: str, **_kwargs: Any):
            captured["url"] = url
            raise RuntimeError("stop before any network use")

        with patch("auto_browser_client.client.httpx.stream", side_effect=fake_stream):
            with self.assertRaises(RuntimeError):
                list(self.client.stream_events("a/b"))

        self.assertEqual(captured["url"], "http://auto-browser.test/sessions/a%2Fb/events")

    def test_close_releases_a_lazily_opened_client(self) -> None:
        client = AutoBrowserClient("http://auto-browser.test")
        client._client()
        opened = client._sync_client
        self.assertIsNotNone(opened)

        client.close()

        self.assertTrue(opened.is_closed)
        self.assertIsNone(client._sync_client)
        client.close()  # idempotent


class AsyncClientPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests: list[httpx.Request] = []
        self.client = AutoBrowserClient("http://auto-browser.test")

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json={"ok": True})

        self.client._async_client = httpx.AsyncClient(
            base_url=self.client.base_url,
            transport=httpx.MockTransport(handler),
        )

    async def asyncTearDown(self) -> None:
        if self.client._async_client is not None:
            await self.client._async_client.aclose()

    async def test_async_actions_and_audit_use_server_routes(self) -> None:
        await self.client.async_navigate("session-1", "https://example.com")
        await self.client.async_click("session-1", element_id="submit")
        await self.client.async_type_text("session-1", "hello", element_id="search")
        await self.client.async_scroll("session-1")
        await self.client.async_list_audit_events(limit=5)

        self.assertEqual(
            [request.url.path for request in self.requests],
            [
                "/sessions/session-1/actions/navigate",
                "/sessions/session-1/actions/click",
                "/sessions/session-1/actions/type",
                "/sessions/session-1/actions/scroll",
                "/audit/events",
            ],
        )

    async def test_async_ids_cannot_retarget_the_request(self) -> None:
        await self.client.async_get_session("../approvals/zz/approve")
        await self.client.async_reject("a/b")

        self.assertEqual(
            [request.url.raw_path.decode() for request in self.requests],
            ["/sessions/..%2Fapprovals%2Fzz%2Fapprove", "/approvals/a%2Fb/reject"],
        )

    async def test_aclose_releases_a_lazily_opened_client(self) -> None:
        client = AutoBrowserClient("http://auto-browser.test")
        client._aclient()
        opened = client._async_client
        self.assertIsNotNone(opened)

        await client.aclose()

        self.assertTrue(opened.is_closed)
        self.assertIsNone(client._async_client)
