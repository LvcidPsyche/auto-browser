"""Tests for the auto-browser LangChain / LangGraph integration.

This package is published to PyPI as ``auto-browser-langchain`` and had no
tests at all. It is also the thinnest surface in the repo — every method is a
small HTTP call plus response unwrapping — which is exactly where a silent
shape change (``content[0].text`` moving, an error flag renamed) goes unnoticed
until a user's agent starts returning empty strings.
"""

from __future__ import annotations

import json
import unittest

import httpx
import respx
from auto_browser_langchain import AutoBrowserNode, AutoBrowserTool

BASE_URL = "http://auto-browser.test"


def _content(text: str) -> dict:
    return {"content": [{"text": text}]}


class AutoBrowserToolTests(unittest.TestCase):
    @respx.mock
    def test_run_executes_synchronously(self) -> None:
        # Regression: _run used asyncio.get_event_loop().run_until_complete(),
        # which raises RuntimeError on Python 3.14 ("no current event loop") --
        # breaking the sync path LangChain and CrewAI call, on a version this
        # package's metadata claims to support.
        route = respx.post(f"{BASE_URL}/mcp/tools/call").mock(
            return_value=httpx.Response(200, json=_content("observed"))
        )

        result = AutoBrowserTool(base_url=BASE_URL)._run("browser.observe", {"session_id": "s1"})

        self.assertEqual(result, "observed")
        self.assertTrue(route.called)

    @respx.mock
    def test_run_defaults_arguments_to_an_empty_object(self) -> None:
        route = respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=_content("ok")))

        AutoBrowserTool(base_url=BASE_URL)._run("browser.observe")

        self.assertEqual(json.loads(route.calls[0].request.content)["arguments"], {})


class AutoBrowserToolAsyncTests(unittest.IsolatedAsyncioTestCase):
    @respx.mock
    async def test_arun_returns_the_first_content_text(self) -> None:
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=_content("page title")))

        result = await AutoBrowserTool(base_url=BASE_URL)._arun("browser.observe", {})

        self.assertEqual(result, "page title")

    @respx.mock
    async def test_arun_surfaces_tool_errors(self) -> None:
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(
            return_value=httpx.Response(200, json={"isError": True, "content": [{"text": "session gone"}]})
        )

        result = await AutoBrowserTool(base_url=BASE_URL)._arun("browser.observe", {})

        self.assertEqual(result, "ERROR: session gone")

    @respx.mock
    async def test_arun_falls_back_to_the_raw_payload_without_text(self) -> None:
        payload = {"content": [{"data": "no text field"}]}
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=payload))

        result = await AutoBrowserTool(base_url=BASE_URL)._arun("browser.observe", {})

        self.assertEqual(json.loads(result), payload)

    @respx.mock
    async def test_arun_sends_the_action_and_arguments(self) -> None:
        route = respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=_content("ok")))

        await AutoBrowserTool(base_url=BASE_URL)._arun("browser.click", {"selector": "#go"})

        body = json.loads(route.calls[0].request.content)
        self.assertEqual(body, {"name": "browser.click", "arguments": {"selector": "#go"}})

    @respx.mock
    async def test_arun_sends_bearer_token_when_configured(self) -> None:
        route = respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=_content("ok")))

        await AutoBrowserTool(base_url=BASE_URL, bearer_token="s3cret")._arun("browser.observe", {})

        self.assertEqual(route.calls[0].request.headers["Authorization"], "Bearer s3cret")

    @respx.mock
    async def test_arun_omits_authorization_without_a_token(self) -> None:
        route = respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=_content("ok")))

        await AutoBrowserTool(base_url=BASE_URL)._arun("browser.observe", {})

        self.assertNotIn("authorization", {k.lower() for k in route.calls[0].request.headers})

    @respx.mock
    async def test_arun_raises_on_http_error(self) -> None:
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(401, json={"detail": "nope"}))

        with self.assertRaises(httpx.HTTPStatusError):
            await AutoBrowserTool(base_url=BASE_URL)._arun("browser.observe", {})


class AutoBrowserToolListToolsTests(unittest.TestCase):
    @respx.mock
    def test_list_tools_returns_the_catalogue(self) -> None:
        respx.get(f"{BASE_URL}/mcp/tools").mock(return_value=httpx.Response(200, json=[{"name": "browser.observe"}]))

        self.assertEqual(AutoBrowserTool.list_tools(base_url=BASE_URL), [{"name": "browser.observe"}])

    @respx.mock
    def test_list_tools_sends_bearer_token(self) -> None:
        route = respx.get(f"{BASE_URL}/mcp/tools").mock(return_value=httpx.Response(200, json=[]))

        AutoBrowserTool.list_tools(base_url=BASE_URL, bearer_token="s3cret")

        self.assertEqual(route.calls[0].request.headers["Authorization"], "Bearer s3cret")


class AutoBrowserNodeTests(unittest.TestCase):
    def test_base_url_trailing_slash_is_stripped(self) -> None:
        self.assertEqual(AutoBrowserNode(base_url=f"{BASE_URL}/").base_url, BASE_URL)

    def test_headers_include_bearer_only_when_set(self) -> None:
        self.assertNotIn("Authorization", AutoBrowserNode(base_url=BASE_URL)._headers())
        self.assertEqual(
            AutoBrowserNode(base_url=BASE_URL, bearer_token="s3cret")._headers()["Authorization"],
            "Bearer s3cret",
        )


class AutoBrowserNodeAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.node = AutoBrowserNode(base_url=BASE_URL)

    @respx.mock
    async def test_call_tool_posts_name_and_arguments(self) -> None:
        route = respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json={"ok": True}))

        result = await self.node.call_tool("browser.observe", {"session_id": "s1"})

        self.assertEqual(result, {"ok": True})
        body = json.loads(route.calls[0].request.content)
        self.assertEqual(body, {"name": "browser.observe", "arguments": {"session_id": "s1"}})

    @respx.mock
    async def test_create_session_reads_session_id(self) -> None:
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(
            return_value=httpx.Response(200, json=_content(json.dumps({"session_id": "sess-1"})))
        )

        self.assertEqual(await self.node.create_session(), "sess-1")

    @respx.mock
    async def test_create_session_accepts_id_as_an_alias(self) -> None:
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(
            return_value=httpx.Response(200, json=_content(json.dumps({"id": "sess-2"})))
        )

        self.assertEqual(await self.node.create_session(), "sess-2")

    @respx.mock
    async def test_create_session_forwards_start_url(self) -> None:
        route = respx.post(f"{BASE_URL}/mcp/tools/call").mock(
            return_value=httpx.Response(200, json=_content(json.dumps({"session_id": "sess-3"})))
        )

        await self.node.create_session(start_url="https://example.com")

        body = json.loads(route.calls[0].request.content)
        self.assertEqual(body["arguments"], {"start_url": "https://example.com"})

    @respx.mock
    async def test_create_session_omits_start_url_when_absent(self) -> None:
        route = respx.post(f"{BASE_URL}/mcp/tools/call").mock(
            return_value=httpx.Response(200, json=_content(json.dumps({"session_id": "sess-4"})))
        )

        await self.node.create_session()

        self.assertEqual(json.loads(route.calls[0].request.content)["arguments"], {})

    @respx.mock
    async def test_create_session_raises_when_no_id_is_returned(self) -> None:
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(
            return_value=httpx.Response(200, json=_content(json.dumps({"unexpected": "shape"})))
        )

        with self.assertRaises(KeyError):
            await self.node.create_session()

    @respx.mock
    async def test_observe_parses_the_embedded_json(self) -> None:
        observation = {"url": "https://example.com/a", "screenshot_url": "/artifacts/a.png"}
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(
            return_value=httpx.Response(200, json=_content(json.dumps(observation)))
        )

        self.assertEqual(await self.node.observe("sess-1"), observation)

    @respx.mock
    async def test_run_creates_a_session_when_state_has_none(self) -> None:
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(
            side_effect=[
                httpx.Response(200, json=_content(json.dumps({"session_id": "sess-new"}))),
                httpx.Response(200, json=_content(json.dumps({"url": "https://example.com/x"}))),
            ]
        )

        state = await self.node.run({"goal": "look around"})

        self.assertEqual(state["session_id"], "sess-new")
        self.assertEqual(state["current_url"], "https://example.com/x")
        self.assertEqual(state["goal"], "look around")

    @respx.mock
    async def test_run_reuses_an_existing_session(self) -> None:
        route = respx.post(f"{BASE_URL}/mcp/tools/call").mock(
            return_value=httpx.Response(200, json=_content(json.dumps({"url": "https://example.com/y"})))
        )

        state = await self.node.run({"session_id": "sess-existing"})

        # Exactly one call: observe. No session was created.
        self.assertEqual(len(route.calls), 1)
        self.assertEqual(json.loads(route.calls[0].request.content)["name"], "browser.observe")
        self.assertEqual(state["session_id"], "sess-existing")

    @respx.mock
    async def test_run_defaults_missing_observation_fields_to_empty_strings(self) -> None:
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=_content(json.dumps({}))))

        state = await self.node.run({"session_id": "sess-1"})

        self.assertEqual(state["current_url"], "")
        self.assertEqual(state["screenshot_url"], "")


if __name__ == "__main__":
    unittest.main()
