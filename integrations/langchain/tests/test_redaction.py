"""R5C-20: the tool's and the node's errors never show the controller URL's secrets or the bearer token.

httpx's raise_for_status names the request URL whole, its password, a token in its query and its fragment included,
and an agent shown the error (LangGraph's ToolNode hands it the exception's text) read them. A token httpx cannot put
in a header failed the request with httpx's error, which quoted a token holding a line break whole and named a
character outside ASCII and its position.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx
import respx
from auto_browser_langchain import AutoBrowserNode, AutoBrowserTool
from auto_browser_langchain.auth import resolve_bearer_token

SECRET_URL = "http://alice:sentinel-pass@auto-browser.test:8000/?key=sentinel-key#sentinel-fragment"
SHOWN = "Client error '401 Unauthorized' for url 'http://auto-browser.test:8000/"
OK = {"content": [{"text": "ok"}]}


def _answer(status: int, body: object) -> respx.Route:
    """Answer every request to auto-browser.test, whatever path httpx makes of a base URL that holds a query."""
    return respx.route(host="auto-browser.test").mock(return_value=httpx.Response(status, json=body))


class HttpErrorTests(unittest.IsolatedAsyncioTestCase):
    def _assert_shown_without_secrets(self, caught: httpx.HTTPStatusError) -> None:
        self.assertTrue(str(caught).startswith(SHOWN), str(caught))
        self.assertNotIn("sentinel", str(caught))
        self.assertEqual(caught.response.status_code, 401)

    @respx.mock
    async def test_the_tools_http_error_never_names_the_urls_secrets(self) -> None:
        _answer(401, {"detail": "nope"})

        with self.assertRaises(httpx.HTTPStatusError) as caught:
            await AutoBrowserTool(base_url=SECRET_URL)._arun("browser.observe", {})

        self._assert_shown_without_secrets(caught.exception)

    @respx.mock
    async def test_the_nodes_http_error_never_names_the_urls_secrets(self) -> None:
        _answer(401, {"detail": "nope"})

        with self.assertRaises(httpx.HTTPStatusError) as caught:
            await AutoBrowserNode(base_url=SECRET_URL).call_tool("browser.observe", {})

        self._assert_shown_without_secrets(caught.exception)

    @respx.mock
    def test_list_tools_http_error_never_names_the_urls_secrets(self) -> None:
        _answer(401, {"detail": "nope"})

        with self.assertRaises(httpx.HTTPStatusError) as caught:
            AutoBrowserTool.list_tools(base_url=SECRET_URL)

        self._assert_shown_without_secrets(caught.exception)

    @respx.mock
    async def test_a_server_error_and_a_redirect_still_raise_and_a_success_does_not(self) -> None:
        tool = AutoBrowserTool(base_url="http://auto-browser.test")
        for status, kind in ((503, "Server error '503 Service Unavailable'"), (302, "Redirect response '302 Found'")):
            with self.subTest(status=status):
                _answer(status, {})
                with self.assertRaises(httpx.HTTPStatusError) as caught:
                    await tool._arun("browser.observe", {})
                self.assertTrue(
                    str(caught.exception).startswith(f"{kind} for url 'http://auto-browser.test/mcp/tools/call'\n")
                )
        _answer(200, OK)
        self.assertEqual(await tool._arun("browser.observe", {}), "ok")


class UnsendableTokenTests(unittest.IsolatedAsyncioTestCase):
    def test_a_token_holding_a_line_break_or_a_character_outside_ascii_is_refused_without_naming_it(self) -> None:
        for token, held in (
            ("sentinelA\nsentinelB", "a line break"),
            ("sentinel" + chr(0xE4), "a character outside ASCII"),
            # Review of c3 (Codex 1): h11 refuses these too, quoting the header whole ("Illegal header value").
            ("sentinelA" + chr(0) + "sentinelB", "a control character"),
            ("sentinelA" + chr(0x0B) + "sentinelB", "a control character"),
            ("sentinelA" + chr(0x0C) + "sentinelB", "a control character"),
        ):
            with self.subTest(held=held):
                with self.assertRaises(ValueError) as caught:
                    resolve_bearer_token(token, None)
                self.assertEqual(str(caught.exception), f"bearer_token holds {held}, which an HTTP header cannot carry")

    def test_a_token_file_holding_one_is_refused_naming_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api-token"
            path.write_text("sentinelA\nsentinelB\n", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                resolve_bearer_token(None, path)

        self.assertIn(f"token_file {path} holds a line break", str(caught.exception))
        self.assertNotIn("sentinel", str(caught.exception))

    async def test_the_tool_refuses_it_before_any_request(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            route = router.route(host="auto-browser.test").mock(return_value=httpx.Response(200, json=OK))
            # httpx's UnicodeEncodeError is a ValueError too, naming the character and its position.
            with self.assertRaises(ValueError) as caught:
                await AutoBrowserTool(base_url="http://auto-browser.test", bearer_token="sentinel" + chr(0x20AC))._arun(
                    "browser.observe", {}
                )
        self.assertFalse(route.called)
        self.assertEqual(
            str(caught.exception), "bearer_token holds a character outside ASCII, which an HTTP header cannot carry"
        )


if __name__ == "__main__":
    unittest.main()
