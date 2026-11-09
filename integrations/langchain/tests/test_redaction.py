"""R5C-20: the tool's and the node's errors never show the controller URL's secrets or the bearer token.

httpx's raise_for_status names the request URL whole, its password, a token in its query and its fragment included,
and an agent shown the error (LangGraph's ToolNode hands it the exception's text) read them. A token httpx cannot put
in a header failed the request with httpx's error, which quoted a token holding a line break whole and named a
character outside ASCII and its position.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import tempfile
import unittest
from pathlib import Path

import httpx
import respx
from auto_browser_langchain import AutoBrowserNode, AutoBrowserTool
from auto_browser_langchain.auth import resolve_bearer_token
from auto_browser_langchain.errors import _redacted, raise_for_status

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

    def test_the_status_error_names_the_url_without_its_userinfo_query_or_fragment(self) -> None:
        # Alone, as errors_shown redacts around it only what it knows of the base URL: a request's own parameters too
        # stay out of the message.
        request = httpx.Request("POST", "http://alice:pw@auto-browser.test:8000/mcp/tools/call?key=value#part")

        with self.assertRaises(httpx.HTTPStatusError) as caught:
            raise_for_status(httpx.Response(401, request=request))

        self.assertIn("for url 'http://auto-browser.test:8000/mcp/tools/call'", str(caught.exception))

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


def _echoing_the_request_line(request: httpx.Request) -> httpx.Response:
    """Fail as httpx fails on an answer it cannot read, whose error quotes what the server sent: the request line."""
    line = f"{request.method} {request.url.raw_path.decode('ascii')} HTTP/1.1"
    raise httpx.RemoteProtocolError(f"illegal status line: bytearray(b'{line}')", request=request)


class TransportErrorTests(unittest.IsolatedAsyncioTestCase):
    """Review of c3 (Codex 2 and 5): httpx's own errors went out as they came, and the one for an answer httpx cannot
    read quotes what the server sent, which can echo the request line (the base URL's query) or a header (the token);
    and a status error's reason phrase is the server's text too."""

    def _assert_raised_without_secrets(self, caught: Exception) -> None:
        self.assertIsInstance(caught, httpx.RemoteProtocolError)
        self.assertNotIn("sentinel", str(caught))
        self.assertIn("[redacted]", str(caught))

    @respx.mock
    async def test_the_tools_and_the_nodes_transport_errors_never_show_the_urls_secrets(self) -> None:
        respx.route(host="auto-browser.test").mock(side_effect=_echoing_the_request_line)
        calls = (
            ("tool", lambda: AutoBrowserTool(base_url=SECRET_URL)._arun("browser.observe", {})),
            ("node", lambda: AutoBrowserNode(base_url=SECRET_URL).call_tool("browser.observe", {})),
        )
        for caller, call in calls:
            with self.subTest(caller=caller):
                with self.assertRaises(httpx.RemoteProtocolError) as caught:
                    await call()
                self._assert_raised_without_secrets(caught.exception)

    @respx.mock
    def test_list_tools_transport_error_never_shows_the_urls_secrets(self) -> None:
        respx.route(host="auto-browser.test").mock(side_effect=_echoing_the_request_line)

        with self.assertRaises(httpx.RemoteProtocolError) as caught:
            AutoBrowserTool.list_tools(base_url=SECRET_URL)

        self._assert_raised_without_secrets(caught.exception)

    @respx.mock
    async def test_a_transport_error_echoing_the_token_never_shows_it(self) -> None:
        def echo_the_token(request: httpx.Request) -> httpx.Response:
            header = request.headers["Authorization"]
            raise httpx.RemoteProtocolError(f"illegal header line: b'Authorization: {header}'", request=request)

        respx.route(host="auto-browser.test").mock(side_effect=echo_the_token)
        tool = AutoBrowserTool(base_url="http://auto-browser.test", bearer_token="sentinel-token-0123456789")

        with self.assertRaises(httpx.RemoteProtocolError) as caught:
            await tool._arun("browser.observe", {})

        self._assert_raised_without_secrets(caught.exception)

    @respx.mock
    async def test_the_nodes_and_list_tools_transport_errors_echoing_the_token_never_show_it(self) -> None:
        def echo_the_token(request: httpx.Request) -> httpx.Response:
            header = request.headers["Authorization"]
            raise httpx.RemoteProtocolError(f"illegal header line: b'Authorization: {header}'", request=request)

        respx.route(host="auto-browser.test").mock(side_effect=echo_the_token)
        url = "http://auto-browser.test"
        token = "sentinel-token-0123456789"
        calls = (
            ("node", lambda: AutoBrowserNode(base_url=url, bearer_token=token).call_tool("browser.observe", {})),
            ("list_tools", lambda: asyncio.to_thread(AutoBrowserTool.list_tools, base_url=url, bearer_token=token)),
        )
        for caller, call in calls:
            with self.subTest(caller=caller):
                with self.assertRaises(httpx.RemoteProtocolError) as caught:
                    await call()
                self._assert_raised_without_secrets(caught.exception)
                self.assertNotIn(token, str(caught.exception))

    @respx.mock
    async def test_a_transport_error_echoing_the_basic_credentials_of_the_userinfo_never_shows_them(self) -> None:
        # Review of c3's fixes (Codex 2): httpx sends a base URL's userinfo as "Authorization: Basic" and the base64 of
        # user:password, each percent-decoded, and a server echoing that header showed the credentials in that form.
        def echo_the_header(request: httpx.Request) -> httpx.Response:
            header = request.headers["Authorization"]
            raise httpx.RemoteProtocolError(
                f"illegal status line: bytearray(b'Authorization: {header}')", request=request
            )

        respx.route(host="auto-browser.test").mock(side_effect=echo_the_header)
        url = "http://al%69ce:sentinel%2Dpass%20word@auto-browser.test:8000/"
        basic = base64.b64encode(b"alice:sentinel-pass word").decode()
        calls = (
            ("tool", lambda: AutoBrowserTool(base_url=url)._arun("browser.observe", {})),
            ("node", lambda: AutoBrowserNode(base_url=url).call_tool("browser.observe", {})),
            ("list_tools", lambda: asyncio.to_thread(AutoBrowserTool.list_tools, base_url=url)),
        )
        for caller, call in calls:
            with self.subTest(caller=caller):
                with self.assertRaises(httpx.RemoteProtocolError) as caught:
                    await call()
                self.assertNotIn(basic, str(caught.exception))
                self.assertIn("Authorization: Basic [redacted]", str(caught.exception))

    @respx.mock
    async def test_a_transport_error_echoing_the_url_as_httpx_sends_it_never_shows_its_secrets(self) -> None:
        # Review of c3's fixes (Codex 3): httpx percent-encodes a space or a character outside ASCII in the query before
        # sending it, and the request line it sent held none of the URL's secrets as they were configured.
        respx.route(host="auto-browser.test").mock(side_effect=_echoing_the_request_line)
        for query in ("key=sentinel value", "key=sentinel" + chr(0xE9) + "value"):
            url = f"http://auto-browser.test:8000/?{query}"
            calls = (
                ("tool", lambda: AutoBrowserTool(base_url=url)._arun("browser.observe", {})),
                ("node", lambda: AutoBrowserNode(base_url=url).call_tool("browser.observe", {})),
                ("list_tools", lambda: asyncio.to_thread(AutoBrowserTool.list_tools, base_url=url)),
            )
            for caller, call in calls:
                with self.subTest(query=query, caller=caller):
                    with self.assertRaises(httpx.RemoteProtocolError) as caught:
                        await call()
                    self._assert_raised_without_secrets(caught.exception)

    @respx.mock
    async def test_a_status_lines_reason_phrase_never_shows_them_either(self) -> None:
        respx.route(host="auto-browser.test").mock(
            return_value=httpx.Response(401, extensions={"reason_phrase": b"bad key sentinel-key"})
        )

        with self.assertRaises(httpx.HTTPStatusError) as caught:
            await AutoBrowserTool(base_url=SECRET_URL)._arun("browser.observe", {})

        self.assertTrue(str(caught.exception).startswith("Client error '401 bad key [redacted]' for url"))
        self.assertNotIn("sentinel", str(caught.exception))
        self.assertEqual(caught.exception.response.status_code, 401)


@unittest.skipUnless(importlib.util.find_spec("auto_browser_client"), "the client package is not installed here")
class RedactionCopyTests(unittest.TestCase):
    """errors._redacted is a copy of the MCP bridges' redaction, since this package depends on httpx only: the two
    must show a text the same way."""

    def test_the_copy_shows_a_text_as_the_bridges_do(self) -> None:
        from auto_browser_client.mcp_bridge import _redacted as the_bridges

        token = "sentinel-token-0123456789"
        cases = (
            (f"upstream {SECRET_URL} refused {token}", SECRET_URL),
            ("bad key sentinel-key, short key abc", "http://h/mcp?key=sentinel-key&page=abc"),
            ("x u:a y getaddrinfo failed", "http://u:a@h/mcp"),  # a short secret, not again inside [redacted]
            ("{'upstream': 'key=sentinel\\\\tail'}", "http://h/mcp?key=sentinel%5Ctail"),
            ('detail {"key": "sentinel tail \\u00e9"}', "http://h/mcp?key=sentinel+tail+%C3%A9"),
            ("see http://controller.example/a@b/mcp and bob@host", "http://controller.example/a@b/mcp"),
            # Review of c3's fixes (Codex 2): the Basic credentials httpx sends for the userinfo.
            (
                "Authorization: Basic " + base64.b64encode(b"alice:sentinel pass").decode(),
                "http://alice:sentinel%20pass@h/mcp",
            ),
            # Review of c3's fixes (Codex 3): the URL as configured and as httpx sends it, each form's secrets counted.
            (
                "POST /mcp?key=sentinel%20value HTTP/1.1",
                ("http://h/mcp?key=sentinel value", "http://h/mcp?key=sentinel%20value"),
            ),
        )
        for text, url in cases:
            with self.subTest(text=text, url=url):
                self.assertEqual(_redacted(text, url, token), the_bridges(text, url, token))
                self.assertNotIn("sentinel", _redacted(text, url, token))


class ShortestRedactedQueryValueTests(unittest.TestCase):
    def test_a_query_value_of_8_characters_is_redacted_and_one_of_7_is_kept(self) -> None:
        # c3 left the boundary unpinned: every query value the other tests use is longer than 8 characters.
        url = "http://h/mcp?short=seven77&long=eight888"

        self.assertEqual(_redacted("short seven77, long eight888", url, None), "short seven77, long [redacted]")


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

    def test_a_token_file_that_is_not_utf_8_is_refused_without_its_bytes(self) -> None:
        # Review of c3's fixes (Codex 5): the UnicodeDecodeError named one of the token's bytes and its position
        # ("can't decode byte 0x80 in position 15"), as the MCP bridges' answer did before c3.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api-token"
            path.write_bytes(b"sentinel-prefix" + bytes([0x80]) + b"sentinel-tail")
            with self.assertRaises(ValueError) as caught:
                resolve_bearer_token(None, path)

        self.assertEqual(str(caught.exception), f"token_file {path} is not UTF-8 text")
        self.assertNotIsInstance(caught.exception, UnicodeDecodeError)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

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
