"""R5C-20: what the client SDK raises never shows the controller URL's secrets or the bearer token.

AutoBrowserError quoted a server's error detail as it came, so a proxy's or gateway's detail echoing the URL (its
password, a token in its query) or the bearer token reached whatever showed the error to an agent. And a token httpx
cannot put in a header failed the first request with httpx's error, which quoted a token holding a line break whole
(h11's "Illegal header value") and named a character outside ASCII and its position.
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from auto_browser_client import AutoBrowserClient
from auto_browser_client.client import AutoBrowserError

TOKEN = "sentinel-token-0123456789"
BASE_URL = "http://alice:sentinel-pass@auto-browser.test:8000/?key=sentinel-key-0123"
SHOWN_URL = "http://auto-browser.test:8000/?[redacted]"


def _answering(response: httpx.Response, **options: Any) -> AutoBrowserClient:
    client = AutoBrowserClient(BASE_URL, **(options or {"token": TOKEN}))
    client._sync_client = httpx.Client(
        base_url=client.base_url, headers=client._headers, transport=httpx.MockTransport(lambda _request: response)
    )
    client._async_client = httpx.AsyncClient(
        base_url=client.base_url, headers=client._headers, transport=httpx.MockTransport(lambda _request: response)
    )
    return client


def _failing(text: str, base_url: str = BASE_URL) -> AutoBrowserClient:
    """A client whose every request fails as httpx fails on an answer it cannot read: a RemoteProtocolError whose text
    quotes what the server sent (h11's "illegal status line"), here ``text`` with the request line filled in."""

    def fail(request: httpx.Request) -> httpx.Response:
        line = f"{request.method} {request.url.raw_path.decode('ascii')} HTTP/1.1"
        raise httpx.RemoteProtocolError(text.format(request_line=line), request=request)

    client = AutoBrowserClient(base_url, token=TOKEN)
    client._sync_client = httpx.Client(
        base_url=client.base_url, headers=client._headers, transport=httpx.MockTransport(fail)
    )
    client._async_client = httpx.AsyncClient(
        base_url=client.base_url, headers=client._headers, transport=httpx.MockTransport(fail)
    )
    return client


# What a server that echoes the request back can make httpx's error quote: the request line (the base URL's query
# and the path httpx put after it) or a header (the token).
ECHOES = ("illegal status line: bytearray(b'{request_line}')", f"illegal header line: b'Authorization: Bearer {TOKEN}'")


class TransportErrorTests(unittest.IsolatedAsyncioTestCase):
    """Review of c3 (Codex 2): httpx's own errors went out as they came, and the one for an answer httpx cannot read
    quotes what the server sent, which can echo the request line or a header."""

    def _assert_raised_without_secrets(self, caught: Exception) -> None:
        self.assertIsInstance(caught, httpx.RemoteProtocolError)
        self.assertNotIn("sentinel", str(caught))
        self.assertIn("[redacted]", str(caught))
        self.assertEqual(caught.request.url.host, "auto-browser.test")  # type: ignore[attr-defined]

    def test_a_transport_error_echoing_the_url_or_the_token_is_raised_without_them(self) -> None:
        for text in ECHOES:
            with self.subTest(text=text):
                with self.assertRaises(httpx.RemoteProtocolError) as caught:
                    _failing(text).create_session(name="s")
                self._assert_raised_without_secrets(caught.exception)

    async def test_the_async_calls_raise_it_without_them_too(self) -> None:
        with self.assertRaises(httpx.RemoteProtocolError) as caught:
            await _failing(ECHOES[0]).async_create_session(name="s")

        self._assert_raised_without_secrets(caught.exception)

    def test_the_event_stream_raises_it_without_them_too(self) -> None:
        # stream_events builds a client of its own, which a loopback base URL gives httpx.HTTPTransport.
        def fail(request: httpx.Request) -> httpx.Response:
            line = f"{request.method} {request.url.raw_path.decode('ascii')} HTTP/1.1"
            raise httpx.RemoteProtocolError(ECHOES[0].format(request_line=line), request=request)

        client = AutoBrowserClient("http://127.0.0.1:9/?key=sentinel-key-0123", token=TOKEN)
        with patch.object(httpx, "HTTPTransport", lambda: httpx.MockTransport(fail)):
            with self.assertRaises(httpx.RemoteProtocolError) as caught:
                list(client.stream_events("s"))

        self.assertNotIn("sentinel", str(caught.exception))
        self.assertIn("GET /?[redacted]/sessions/s/events", str(caught.exception))

    def test_a_transport_error_echoing_the_basic_credentials_of_the_userinfo_is_raised_without_them(self) -> None:
        # Review of c3's fixes (Codex 2): httpx sends a base URL's userinfo as "Authorization: Basic" and the base64 of
        # user:password, each percent-decoded, and a server echoing that header showed the credentials in that form.
        def echo_the_header(request: httpx.Request) -> httpx.Response:
            header = request.headers["Authorization"]
            raise httpx.RemoteProtocolError(
                f"illegal status line: bytearray(b'Authorization: {header}')", request=request
            )

        for base_url, credentials in (
            ("http://alice:sentinel-pass@auto-browser.test:8000/", b"alice:sentinel-pass"),
            ("http://al%69ce:sentinel%2Dpass%20word@auto-browser.test:8000/", b"alice:sentinel-pass word"),
        ):
            with self.subTest(base_url=base_url):
                client = AutoBrowserClient(base_url)
                client._sync_client = httpx.Client(
                    base_url=client.base_url, headers=client._headers, transport=httpx.MockTransport(echo_the_header)
                )
                with self.assertRaises(httpx.RemoteProtocolError) as caught:
                    client.create_session(name="s")
                self.assertNotIn(base64.b64encode(credentials).decode(), str(caught.exception))
                self.assertIn("Authorization: Basic [redacted]", str(caught.exception))

    def test_a_transport_error_echoing_the_url_as_httpx_sends_it_is_raised_without_its_secrets(self) -> None:
        # Review of c3's fixes (Codex 3): httpx percent-encodes a space or a character outside ASCII in the query before
        # sending it, and the request line it sent held none of the URL's secrets as they were configured.
        for query in ("key=sentinel value", "key=sentinel" + chr(0xE9) + "value"):
            with self.subTest(query=query):
                with self.assertRaises(httpx.RemoteProtocolError) as caught:
                    _failing(ECHOES[0], f"http://auto-browser.test:8000/?{query}").create_session(name="s")
                self._assert_raised_without_secrets(caught.exception)

    def test_an_error_that_quotes_neither_is_raised_as_it_came(self) -> None:
        error = httpx.ConnectError("[Errno 111] Connection refused")
        client = AutoBrowserClient(BASE_URL, token=TOKEN)

        def fail(request: httpx.Request) -> httpx.Response:
            raise error

        client._sync_client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(fail))
        with self.assertRaises(httpx.ConnectError) as caught:
            client.health()

        self.assertIs(caught.exception, error)


class ErrorDetailTests(unittest.IsolatedAsyncioTestCase):
    def _assert_raised_without_secrets(self, caught: AutoBrowserError, shown: Any) -> None:
        self.assertEqual(caught.status_code, 502)
        self.assertEqual(caught.detail, shown)
        self.assertNotIn("sentinel", str(caught))
        self.assertNotIn("sentinel", json.dumps(caught.detail))

    def test_a_detail_echoing_the_url_or_the_token_is_raised_without_them(self) -> None:
        echoed = f"upstream {BASE_URL} refused {TOKEN}"
        cases = (
            (httpx.Response(502, json={"detail": echoed}), {"detail": f"upstream {SHOWN_URL} refused [redacted]"}),
            (
                httpx.Response(502, text=f"<h1>Bad gateway</h1> {echoed}"),
                f"<h1>Bad gateway</h1> upstream {SHOWN_URL} refused [redacted]",
            ),
            # FastAPI's 422 shape quotes a query value alone.
            (
                httpx.Response(502, json={"detail": [{"input": "sentinel-key-0123"}]}),
                {"detail": [{"input": "[redacted]"}]},
            ),
        )
        for response, shown in cases:
            with self.subTest(shown=shown):
                with self.assertRaises(AutoBrowserError) as caught:
                    _answering(response).health()
                self._assert_raised_without_secrets(caught.exception, shown)

    def test_a_detail_echoing_an_authorization_header_given_in_headers_is_raised_without_it(self) -> None:
        # Review of c3 (Codex 3): only token= and token_file= were left out, and httpx sends this header the same way.
        client = _answering(
            httpx.Response(502, json={"detail": "bad token sentinel-from-headers"}),
            headers={"Authorization": "Bearer sentinel-from-headers"},
        )

        with self.assertRaises(AutoBrowserError) as caught:
            client.health()

        self._assert_raised_without_secrets(caught.exception, {"detail": "bad token [redacted]"})

    def test_a_detail_quoting_the_url_as_httpx_sends_it_is_raised_without_its_secrets(self) -> None:
        # Review of c3's fixes (Codex 3): a server quotes the request line as it got it, percent-encoded by httpx.
        response = httpx.Response(502, json={"detail": "no route for POST /?key=sentinel%20value/sessions"})
        client = AutoBrowserClient("http://auto-browser.test:8000/?key=sentinel value", token=TOKEN)
        client._sync_client = httpx.Client(
            base_url=client.base_url, headers=client._headers, transport=httpx.MockTransport(lambda _request: response)
        )

        with self.assertRaises(AutoBrowserError) as caught:
            client.create_session(name="s")

        self._assert_raised_without_secrets(caught.exception, {"detail": "no route for POST /?[redacted]/sessions"})

    async def test_the_async_calls_raise_the_detail_without_them_too(self) -> None:
        client = _answering(httpx.Response(502, json={"detail": f"refused {TOKEN}"}))

        with self.assertRaises(AutoBrowserError) as caught:
            await client.async_health()

        self._assert_raised_without_secrets(caught.exception, {"detail": "refused [redacted]"})


class UnsendableTokenTests(unittest.TestCase):
    """httpx raised from the first request instead, quoting the header or naming the character."""

    def test_a_token_holding_a_line_break_or_a_character_outside_ascii_is_refused_without_naming_it(self) -> None:
        for token, held in (
            ("sentinelA\nsentinelB", "a line break"),
            ("sentinelA\rsentinelB", "a line break"),
            ("sentinel" + chr(0xE4), "a character outside ASCII"),
            ("sentinel" + chr(0x20AC), "a character outside ASCII"),
            # Review of c3 (Codex 1): h11 refuses these too, quoting the header whole ("Illegal header value").
            ("sentinelA" + chr(0) + "sentinelB", "a control character"),
            ("sentinelA" + chr(0x0B) + "sentinelB", "a control character"),
            ("sentinelA" + chr(0x0C) + "sentinelB", "a control character"),
            ("sentinelA" + chr(0x7F) + "sentinelB", "a control character"),
        ):
            with self.subTest(held=held, token=repr(token)):
                with self.assertRaises(ValueError) as caught:
                    AutoBrowserClient("http://auto-browser.test", token=token)
                self.assertEqual(str(caught.exception), f"token holds {held}, which an HTTP header cannot carry")

    def test_a_token_holding_a_tab_is_still_sent(self) -> None:
        # A tab inside a header's value is the one control character HTTP allows, and h11 sends it.
        client = AutoBrowserClient("http://auto-browser.test", token="sentinelA\tsentinelB")

        self.assertEqual(client._headers["Authorization"], "Bearer sentinelA\tsentinelB")

    def test_an_authorization_header_given_in_headers_is_refused_as_a_token_is(self) -> None:
        # Review of c3 (Codex 3): httpx sends it as it sends the token's, and quoted it the same way.
        with self.assertRaises(ValueError) as caught:
            AutoBrowserClient("http://auto-browser.test", headers={"authorization": "Bearer sentinelA\nsentinelB"})

        self.assertEqual(
            str(caught.exception),
            "the Authorization header in headers holds a line break, which an HTTP header cannot carry",
        )

    def test_several_authorization_headers_in_headers_are_refused_without_quoting_them(self) -> None:
        # Review of c3's fixes (Codex 1): `headers` can hold one per case of the name, and httpx sends each; only the
        # first was checked and redacted, so h11 quoted the second whole for its NUL ("Illegal header value").
        headers = {"authorization": "Bearer sentinel-first", "Authorization": "Bearer sentinel" + chr(0) + "tail"}

        with self.assertRaises(ValueError) as caught:
            AutoBrowserClient("http://auto-browser.test", headers=headers)

        self.assertEqual(
            str(caught.exception), "headers holds more than one Authorization header, and httpx sends each; keep one"
        )

    def test_a_token_replaces_every_authorization_header_given_in_headers(self) -> None:
        # Review of c3's fixes (Codex 1): one whose name differed in case from "Authorization" was sent beside the
        # token's, its credentials not left out of the errors.
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "api-token"
            token_file.write_text(TOKEN + "\n", encoding="utf-8")
            for options in ({"token": TOKEN}, {"token_file": token_file}):
                with self.subTest(given=next(iter(options))):
                    sent: list[list[str]] = []

                    def answer(request: httpx.Request) -> httpx.Response:
                        sent.append(request.headers.get_list("Authorization"))
                        return httpx.Response(200, json={"ok": True})

                    client = AutoBrowserClient(
                        "http://auto-browser.test",
                        headers={"authorization": "Bearer sentinel-lower", "AUTHORIZATION": "Bearer sentinel-upper"},
                        **options,
                    )
                    client._sync_client = httpx.Client(
                        base_url=client.base_url, headers=client._headers, transport=httpx.MockTransport(answer)
                    )
                    client.health()

                    self.assertEqual(sent, [[f"Bearer {TOKEN}"]])

    def test_a_token_file_holding_one_is_refused_naming_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api-token"
            path.write_text("sentinelA\nsentinelB\n", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                AutoBrowserClient("http://auto-browser.test", token_file=path)

        self.assertIn(f"token_file {path} holds a line break", str(caught.exception))
        self.assertNotIn("sentinel", str(caught.exception))

    def test_a_token_file_that_is_not_utf_8_is_refused_without_its_bytes(self) -> None:
        # Review of c3's fixes (Codex 5): the UnicodeDecodeError named one of the token's bytes and its position
        # ("can't decode byte 0x80 in position 15"), as the MCP bridges' answer did before c3.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api-token"
            path.write_bytes(b"sentinel-prefix" + bytes([0x80]) + b"sentinel-tail")
            with self.assertRaises(ValueError) as caught:
                AutoBrowserClient("http://auto-browser.test", token_file=path)

        self.assertEqual(str(caught.exception), f"token_file {path} is not UTF-8 text")
        self.assertNotIsInstance(caught.exception, UnicodeDecodeError)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
