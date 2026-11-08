"""R5C-20: what the client SDK raises never shows the controller URL's secrets or the bearer token.

AutoBrowserError quoted a server's error detail as it came, so a proxy's or gateway's detail echoing the URL (its
password, a token in its query) or the bearer token reached whatever showed the error to an agent. And a token httpx
cannot put in a header failed the first request with httpx's error, which quoted a token holding a line break whole
(h11's "Illegal header value") and named a character outside ASCII and its position.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

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

    def test_a_token_file_holding_one_is_refused_naming_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api-token"
            path.write_text("sentinelA\nsentinelB\n", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                AutoBrowserClient("http://auto-browser.test", token_file=path)

        self.assertIn(f"token_file {path} holds a line break", str(caught.exception))
        self.assertNotIn("sentinel", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
