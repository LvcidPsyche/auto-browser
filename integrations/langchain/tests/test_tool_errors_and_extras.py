"""Tool errors reach callers as one exception, and the package works without langchain-core.

/mcp/tools/call answers HTTP 200 with ``isError: true`` when a tool fails, so
``raise_for_status`` never fires. AutoBrowserNode used to ``json.loads`` the error
text (JSONDecodeError for "Tool execution failed"), or read an error payload as
a result (``run`` returned an empty state and the error vanished).
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import unittest

import httpx
import respx
from auto_browser_langchain import AutoBrowserNode, AutoBrowserToolError

BASE_URL = "http://auto-browser.test"

# The wire shapes below are McpToolCallResponse(...).model_dump(exclude_none=True,
# by_alias=True) from controller/app/models.py, as the gateway builds them in
# controller/app/tool_gateway/gateway.py (_error_response, the SessionNotFoundError
# branch and the BrowserActionError branch). Copied as literals: this package's
# tests must not import the controller.
_BAE_PAYLOAD = {
    "ok": False,
    "error": "selector not found",
    "code": "selector_not_found",
    "action": "click",
    "retryable": False,
    "url": None,
}
PLAIN_TEXT_ERROR = {
    "content": [{"type": "text", "text": "Tool execution failed"}],
    "structuredContent": {"error": "Tool execution failed"},
    "isError": True,
}
SESSION_NOT_FOUND = {
    "content": [{"type": "text", "text": "No session with id s1. List sessions to find a live one."}],
    "structuredContent": {
        "error": "No session with id s1. List sessions to find a live one.",
        "code": "unknown_session",
        "session_id": "s1",
    },
    "isError": True,
}
BROWSER_ACTION_ERROR = {
    "content": [{"type": "text", "text": json.dumps(_BAE_PAYLOAD)}],
    "structuredContent": _BAE_PAYLOAD,
    "isError": True,
}
# A controller that sends the error payload only as JSON text, with no structuredContent.
JSON_TEXT_ERROR_WITHOUT_STRUCTURED = {
    "content": [{"type": "text", "text": json.dumps(_BAE_PAYLOAD)}],
    "isError": True,
}

ERROR_RESPONSES = {
    "plain text": PLAIN_TEXT_ERROR,
    "session not found": SESSION_NOT_FOUND,
    "browser action error": BROWSER_ACTION_ERROR,
    "json text only": JSON_TEXT_ERROR_WITHOUT_STRUCTURED,
}


class NodeToolErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.node = AutoBrowserNode(base_url=BASE_URL)

    @respx.mock
    async def test_every_node_call_raises_the_package_error_on_iserror(self) -> None:
        calls = {
            "observe": ("browser.observe", lambda: self.node.observe("s1")),
            "create_session": ("browser.create_session", lambda: self.node.create_session()),
            "run": ("browser.observe", lambda: self.node.run({"session_id": "s1"})),
            "run without session": ("browser.create_session", lambda: self.node.run({})),
        }
        for response_name, body in ERROR_RESPONSES.items():
            respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=body))
            for call_name, (tool, call) in calls.items():
                with self.subTest(response=response_name, call=call_name):
                    with self.assertRaises(AutoBrowserToolError) as ctx:
                        await call()
                    self.assertEqual(ctx.exception.tool, tool)

    @respx.mock
    async def test_the_error_carries_the_text_and_code_to_tell_failures_apart(self) -> None:
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=PLAIN_TEXT_ERROR))
        with self.assertRaises(AutoBrowserToolError) as plain:
            await self.node.observe("s1")
        self.assertEqual(plain.exception.message, "Tool execution failed")
        self.assertEqual(plain.exception.payload, {"error": "Tool execution failed"})
        self.assertIsNone(plain.exception.code)
        self.assertIsNone(plain.exception.retryable)
        self.assertIn("browser.observe", str(plain.exception))
        self.assertIn("Tool execution failed", str(plain.exception))

        respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=SESSION_NOT_FOUND))
        with self.assertRaises(AutoBrowserToolError) as missing:
            await self.node.observe("s1")
        self.assertEqual(missing.exception.code, "unknown_session")
        self.assertEqual(missing.exception.payload["session_id"], "s1")

        respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=BROWSER_ACTION_ERROR))
        with self.assertRaises(AutoBrowserToolError) as selector:
            await self.node.observe("s1")
        self.assertEqual(selector.exception.code, "selector_not_found")
        self.assertIs(selector.exception.retryable, False)
        self.assertEqual(selector.exception.message, "selector not found")

    @respx.mock
    async def test_a_json_text_error_without_structured_content_still_has_its_payload(self) -> None:
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(
            return_value=httpx.Response(200, json=JSON_TEXT_ERROR_WITHOUT_STRUCTURED)
        )
        with self.assertRaises(AutoBrowserToolError) as ctx:
            await self.node.observe("s1")
        self.assertEqual(ctx.exception.code, "selector_not_found")
        self.assertEqual(ctx.exception.message, "selector not found")

    @respx.mock
    async def test_a_non_object_structured_content_is_not_trusted_as_a_payload(self) -> None:
        body = {"content": [{"type": "text", "text": "boom"}], "structuredContent": "boom", "isError": True}
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=body))
        with self.assertRaises(AutoBrowserToolError) as ctx:
            await self.node.observe("s1")
        self.assertEqual(ctx.exception.message, "boom")
        self.assertIsNone(ctx.exception.payload)

    @respx.mock
    async def test_an_error_without_any_text_says_so(self) -> None:
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(
            return_value=httpx.Response(200, json={"isError": True, "content": []})
        )
        with self.assertRaises(AutoBrowserToolError) as ctx:
            await self.node.observe("s1")
        self.assertEqual(ctx.exception.message, "Unknown error")

    @respx.mock
    async def test_a_successful_result_with_object_structured_content_still_parses(self) -> None:
        # The controller sends structuredContent on success too (an external change
        # makes it always an object); the node must keep reading content[0].text.
        observation = {"url": "https://example.com/a", "screenshot_url": "/artifacts/a.png"}
        body = {
            "content": [{"type": "text", "text": json.dumps(observation)}],
            "structuredContent": observation,
            "isError": False,
        }
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=body))
        self.assertEqual(await self.node.observe("s1"), observation)

    @respx.mock
    async def test_a_successful_non_json_text_result_is_returned_not_a_crash(self) -> None:
        body = {"content": [{"type": "text", "text": "page is still loading"}], "isError": False}
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=body))

        self.assertEqual(await self.node.observe("s1"), {"text": "page is still loading"})
        state = await self.node.run({"session_id": "s1"})
        self.assertEqual(state["current_url"], "")
        with self.assertRaises(KeyError):
            await self.node.create_session()

    @respx.mock
    async def test_a_successful_json_result_that_is_not_an_object_is_wrapped(self) -> None:
        body = {"content": [{"type": "text", "text": json.dumps([1, 2])}], "isError": False}
        respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=body))

        self.assertEqual(await self.node.observe("s1"), {"result": [1, 2]})


# Runs in a child process so langchain_core can be made unimportable even though the
# test environment has it installed: a meta-path finder raises on any langchain_core import.
_BLOCKED_PRELUDE = textwrap.dedent(
    """
    import importlib.abc, sys

    class Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "langchain_core" or name.startswith("langchain_core."):
                raise ImportError("blocked: " + name)

    sys.meta_path.insert(0, Blocker())
    """
)


def _run_without_langchain_core(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _BLOCKED_PRELUDE + textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=60,
    )


class WithoutLangchainCoreTests(unittest.TestCase):
    def test_building_the_tool_names_the_extra_to_install(self) -> None:
        result = _run_without_langchain_core(
            """
            from auto_browser_langchain import AutoBrowserTool
            try:
                AutoBrowserTool(base_url="http://x", bearer_token="t")
            except ImportError as exc:
                print(exc)
                print(type(exc.__cause__).__name__)
            else:
                raise SystemExit("AutoBrowserTool() did not raise")
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('pip install "auto-browser-langchain[langchain]"', result.stdout)
        self.assertIn("langchain-core", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], "ImportError")  # the blocked import is chained

    def test_the_package_node_and_tool_catalogue_still_work(self) -> None:
        result = _run_without_langchain_core(
            """
            import auto_browser_langchain as pkg
            node = pkg.AutoBrowserNode(base_url="http://x/", bearer_token="t")
            assert node.base_url == "http://x"
            assert node._headers()["Authorization"] == "Bearer t"
            assert callable(pkg.AutoBrowserTool.list_tools)
            assert issubclass(pkg.AutoBrowserToolError, RuntimeError)
            print("ok")
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_the_tool_still_builds_when_langchain_core_is_present(self) -> None:
        from auto_browser_langchain import AutoBrowserTool

        tool = AutoBrowserTool(base_url="http://x", bearer_token="t")
        self.assertEqual(tool.base_url, "http://x")


if __name__ == "__main__":
    unittest.main()
