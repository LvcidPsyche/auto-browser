"""token_file: the tool, list_tools and the node read the controller's token from a file.

A local controller with no API_BEARER_TOKEN writes its generated token to
data/api-token in the auto-browser folder. The file is read on every call, so a
tool built before the controller first started, or kept across a regenerated
token, sends the current one.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx
import respx
from auto_browser_langchain import AutoBrowserNode, AutoBrowserTool
from auto_browser_langchain.auth import resolve_bearer_token

BASE_URL = "http://auto-browser.test"
OK = {"content": [{"text": "ok"}]}


class _TokenFileCase:
    def make_token_file(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)  # type: ignore[attr-defined]
        return Path(directory.name) / "api-token"


class ResolveBearerTokenTests(_TokenFileCase, unittest.TestCase):
    def test_a_token_wins_and_is_stripped(self) -> None:
        path = self.make_token_file()
        path.write_text("from-file\n", encoding="utf-8")
        self.assertEqual(resolve_bearer_token(" given\n", path), "given")

    def test_the_file_is_used_without_a_token(self) -> None:
        path = self.make_token_file()
        path.write_text("  from-file\n", encoding="utf-8")
        self.assertEqual(resolve_bearer_token(None, path), "from-file")
        self.assertEqual(resolve_bearer_token("  ", str(path)), "from-file")

    def test_neither_means_no_token(self) -> None:
        self.assertIsNone(resolve_bearer_token(None, None))
        self.assertIsNone(resolve_bearer_token("  ", None))

    def test_a_missing_file_names_the_path(self) -> None:
        path = self.make_token_file()
        with self.assertRaises(FileNotFoundError) as caught:
            resolve_bearer_token(None, path)
        self.assertIn(str(path), str(caught.exception))

    def test_an_empty_file_is_an_error(self) -> None:
        path = self.make_token_file()
        path.write_text("\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            resolve_bearer_token(None, path)
        self.assertIn(str(path), str(caught.exception))


class ToolTokenFileTests(_TokenFileCase, unittest.IsolatedAsyncioTestCase):
    @respx.mock
    async def test_arun_reads_the_file_on_every_call(self) -> None:
        route = respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=OK))
        path = self.make_token_file()
        tool = AutoBrowserTool(base_url=BASE_URL, token_file=str(path))

        path.write_text("first\n", encoding="utf-8")
        await tool._arun("browser.observe", {})
        path.write_text("second\n", encoding="utf-8")
        await tool._arun("browser.observe", {})

        self.assertEqual(route.calls[0].request.headers["Authorization"], "Bearer first")
        self.assertEqual(route.calls[1].request.headers["Authorization"], "Bearer second")

    @respx.mock
    async def test_arun_prefers_the_bearer_token(self) -> None:
        route = respx.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=OK))
        path = self.make_token_file()
        path.write_text("from-file\n", encoding="utf-8")

        await AutoBrowserTool(base_url=BASE_URL, bearer_token="s3cret", token_file=str(path))._arun("x", {})

        self.assertEqual(route.calls[0].request.headers["Authorization"], "Bearer s3cret")

    async def test_arun_with_a_missing_file_raises_before_any_request(self) -> None:
        path = self.make_token_file()
        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{BASE_URL}/mcp/tools/call").mock(return_value=httpx.Response(200, json=OK))
            with self.assertRaises(FileNotFoundError):
                await AutoBrowserTool(base_url=BASE_URL, token_file=str(path))._arun("x", {})
        self.assertFalse(route.called)


class ListToolsTokenFileTests(_TokenFileCase, unittest.TestCase):
    @respx.mock
    def test_list_tools_sends_the_token_from_the_file(self) -> None:
        route = respx.get(f"{BASE_URL}/mcp/tools").mock(return_value=httpx.Response(200, json=[]))
        path = self.make_token_file()
        path.write_text("from-file\n", encoding="utf-8")

        AutoBrowserTool.list_tools(base_url=BASE_URL, token_file=path)

        self.assertEqual(route.calls[0].request.headers["Authorization"], "Bearer from-file")


class NodeTokenFileTests(_TokenFileCase, unittest.TestCase):
    def test_headers_read_the_file_on_every_call(self) -> None:
        path = self.make_token_file()
        node = AutoBrowserNode(base_url=BASE_URL, token_file=path)

        path.write_text("first\n", encoding="utf-8")
        self.assertEqual(node._headers()["Authorization"], "Bearer first")
        path.write_text("second\n", encoding="utf-8")
        self.assertEqual(node._headers()["Authorization"], "Bearer second")

    def test_a_bearer_token_wins(self) -> None:
        path = self.make_token_file()
        path.write_text("from-file\n", encoding="utf-8")
        node = AutoBrowserNode(base_url=BASE_URL, bearer_token="s3cret", token_file=path)
        self.assertEqual(node._headers()["Authorization"], "Bearer s3cret")


if __name__ == "__main__":
    unittest.main()
