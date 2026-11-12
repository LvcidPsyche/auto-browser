"""resources/read: -32002 means the resource does not exist, anything else that goes wrong is -32603.

Every failure used to read as "Resource not found" with only a debug line, and the two aggregate
resources were not covered at all (an unhandled 500). The -32603 message is fixed text, because an
exception's own text can carry a page URL with a token in it. A page's DOM has a cap, and says when it was cut.
"""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import app.mcp_transport as mcp_transport
from app.action_errors import SessionNotFoundError
from app.mcp_transport import McpHttpTransport, McpSession
from app.utils import capped_for_log
from tests.test_mcp_resource_ownership import PROTOCOL, mcp_request

SECRET = "tok-s3cr3t-value"


class McpResourceReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.page = SimpleNamespace(content=AsyncMock(return_value="<html></html>"))
        self.browser_session = SimpleNamespace(id="s1", page=self.page, lock=asyncio.Lock(), last_screenshot=None)
        self.manager = SimpleNamespace(
            sessions={"s1": self.browser_session},
            get_session=AsyncMock(return_value=self.browser_session),
            capture_screenshot=AsyncMock(),
            get_console_messages=AsyncMock(return_value={"items": []}),
            get_network_log=AsyncMock(return_value={"entries": []}),
            list_sessions=AsyncMock(return_value=[]),
            list_audit_events=AsyncMock(return_value=[]),
            _assert_page_allowed=lambda session: None,
            _page_refused=lambda page: False,
            page_provenance=SimpleNamespace(refused_commits=lambda page: 0),
        )
        self.transport = McpHttpTransport(
            tool_gateway=SimpleNamespace(list_tools=lambda: []),
            server_name="auto-browser",
            server_version="0",
            manager=self.manager,
        )
        self.transport._sessions["m1"] = McpSession(
            id="m1", protocol_version=PROTOCOL, client_info={}, client_capabilities={}, initialized=True
        )

    async def read(self, uri: str) -> dict:
        body = {"jsonrpc": "2.0", "id": 7, "method": "resources/read", "params": {"uri": uri}}
        response = await self.transport.handle_post_request(mcp_request("POST", "m1", body))
        self.assertEqual(response.status_code, 200)
        return json.loads(response.body)

    def assert_internal_error(self, answer: dict, uri: str) -> None:
        self.assertEqual(answer["error"]["code"], -32603)
        self.assertEqual(answer["error"]["message"], f"Could not read resource: {uri}")
        self.assertNotIn("result", answer)

    def assert_not_found(self, answer: dict, uri: str) -> None:
        self.assertEqual(answer["error"]["code"], -32002)
        self.assertEqual(answer["error"]["message"], f"Resource not found: {uri}")

    async def test_a_read_that_fails_is_an_internal_error_not_a_missing_resource(self) -> None:
        failures = [
            ("a timeout", "browser://s1/console", "get_console_messages", TimeoutError("timed out")),
            ("a policy refusal", "browser://s1/network", "get_network_log", PermissionError("not on ALLOWED_HOSTS")),
            ("a capture that fails", "browser://s1/screenshot", "capture_screenshot", RuntimeError("no display")),
            ("a page the session lost", "browser://s1/dom", "get_session", RuntimeError("page closed")),
        ]
        for name, uri, method, error in failures:
            with self.subTest(name):
                getattr(self.manager, method).side_effect = error
                with self.assertLogs("app.mcp_transport", "WARNING") as logged:
                    answer = await self.read(uri)

                self.assert_internal_error(answer, uri)
                self.assertIn(repr(uri), logged.output[0])
                self.assertIn(type(error).__name__, logged.output[0])
                getattr(self.manager, method).side_effect = None

    async def test_a_page_that_closes_during_a_dom_read_is_an_internal_error(self) -> None:
        self.page.content.side_effect = RuntimeError("Target page, context or browser has been closed")

        with self.assertLogs("app.mcp_transport", "WARNING"):
            answer = await self.read("browser://s1/dom")

        self.assert_internal_error(answer, "browser://s1/dom")

    async def test_the_aggregate_resources_answer_an_internal_error_too(self) -> None:
        for uri, method in (("browser://sessions", "list_sessions"), ("browser://audit/events", "list_audit_events")):
            with self.subTest(uri=uri):
                getattr(self.manager, method).side_effect = OSError("store unavailable")
                with self.assertLogs("app.mcp_transport", "WARNING") as logged:
                    answer = await self.read(uri)

                self.assert_internal_error(answer, uri)
                self.assertIn("OSError", logged.output[0])

    async def test_a_screenshot_file_that_has_gone_is_an_internal_error(self) -> None:
        gone = Path(self.tempdir.name) / "gone.png"
        self.manager.capture_screenshot.return_value = {"screenshot_path": str(gone)}

        with self.assertLogs("app.mcp_transport", "WARNING") as logged:
            answer = await self.read("browser://s1/screenshot")

        self.assert_internal_error(answer, "browser://s1/screenshot")
        self.assertIn("FileNotFoundError", logged.output[0])

    async def test_a_resource_that_cannot_be_encoded_is_an_internal_error(self) -> None:
        self.manager.get_console_messages.return_value = {"items": [{"text": object()}]}

        with self.assertLogs("app.mcp_transport", "WARNING") as logged:
            answer = await self.read("browser://s1/console")

        self.assert_internal_error(answer, "browser://s1/console")
        self.assertIn("TypeError", logged.output[0])

    async def test_a_dom_holding_a_lone_surrogate_is_read_with_the_replacement_character(self) -> None:
        """A page can put a lone surrogate in its DOM, and UTF-8 cannot carry one: its text answers with U+FFFD."""
        self.page.content.return_value = "<p>\ud83d</p>"

        with self.assertNoLogs("app.mcp_transport", "WARNING"):
            answer = await self.read("browser://s1/dom")

        self.assertEqual(answer["result"]["contents"][0]["text"], "<p>\ufffd</p>")

    async def test_the_exceptions_own_text_reaches_neither_the_client_nor_the_log(self) -> None:
        self.manager.get_session.side_effect = RuntimeError(f"net::ERR_ABORTED at https://x.test/?token={SECRET}")

        with self.assertLogs("app.mcp_transport", "WARNING") as logged:
            answer = await self.read("browser://s1/dom")

        self.assertNotIn(SECRET, json.dumps(answer))
        self.assertNotIn(SECRET, "\n".join(logged.output))
        self.assertIsNone(logged.records[0].exc_info)

    async def test_the_uri_is_logged_quoted_because_it_is_caller_input(self) -> None:
        uri = "browser://s1\nforged line/console"
        self.manager.get_console_messages.side_effect = RuntimeError("no")

        with self.assertLogs("app.mcp_transport", "WARNING") as logged:
            await self.read(uri)

        self.assertIn(repr(uri), logged.output[0])
        self.assertNotIn("\nforged line", logged.records[0].getMessage())

    async def test_a_long_uri_is_logged_cut_as_the_other_lines_cut_a_callers_text(self) -> None:
        # The caller's text, of any length: a URI of 60,000 characters was written whole.
        head = "browser://s1\nforged line "
        uri = head + "s" * (60_000 - len(head) - len("/console")) + "/console"
        self.assertEqual(len(uri), 60_000)
        self.manager.get_console_messages.side_effect = RuntimeError("no")

        with self.assertLogs("app.mcp_transport", "WARNING") as logged:
            answer = await self.read(uri)

        (record,) = logged.records
        line = record.getMessage()
        # Quoted, with the cut mark after the closing quote: the equality also holds the URI's line break as its
        # escape (a check for a line break beside it could not fail, as %r escaped it before the cut too).
        self.assertEqual(line, f"MCP resource read of {uri[:200]!r}... failed: RuntimeError")
        # The answer repeated the URI whole too: it is cut as the line cuts it, in the same code and words.
        self.assert_internal_error(answer, capped_for_log(uri))
        self.assertLess(len(json.dumps(answer)), 1024)

    async def test_an_unknown_resource_or_session_is_still_not_found_and_not_logged(self) -> None:
        self.manager.get_console_messages.side_effect = SessionNotFoundError("bob-session")
        self.manager.capture_screenshot.side_effect = SessionNotFoundError("s9", status="closed")

        with self.assertNoLogs("app.mcp_transport", "WARNING"):
            for uri in (
                "browser://s1/does-not-exist",
                "browser://not-a-session-resource",
                "file:///etc/passwd",
                "browser://bob-session/console",
                "browser://s9/screenshot",
            ):
                with self.subTest(uri=uri):
                    self.assert_not_found(await self.read(uri), uri)

    async def test_another_operators_session_reads_exactly_like_one_that_does_not_exist(self) -> None:
        self.manager.get_session.side_effect = SessionNotFoundError("bob-session")
        bobs = await self.read("browser://bob-session/dom")

        self.manager.get_session.side_effect = SessionNotFoundError("no-such-session", status="closed")
        unknown = await self.read("browser://no-such-session/dom")

        self.assertEqual(bobs["error"]["code"], unknown["error"]["code"])
        self.assertEqual(
            bobs["error"]["message"].replace("bob-session", "X"),
            unknown["error"]["message"].replace("no-such-session", "X"),
        )

    async def test_a_page_at_the_cap_is_returned_whole(self) -> None:
        html = "a" * mcp_transport.MAX_DOM_RESOURCE_CHARS
        self.page.content.return_value = html

        answer = await self.read("browser://s1/dom")

        self.assertEqual(answer["result"]["contents"][0]["text"], html)

    async def test_a_page_over_the_cap_comes_back_at_the_cap_ending_in_a_marker_that_says_so(self) -> None:
        cap = mcp_transport.MAX_DOM_RESOURCE_CHARS
        self.page.content.return_value = "a" * (cap + 1)

        text = (await self.read("browser://s1/dom"))["result"]["contents"][0]["text"]

        marker = text[text.index("<!--") :]
        self.assertEqual(len(text), cap)
        self.assertEqual(text[: -len(marker)], "a" * (cap - len(marker)))
        self.assertTrue(marker.endswith("-->"), marker)
        self.assertIn("cut to", marker)
        self.assertIn(f"{cap:,}", marker)
        self.assertIn(f"{cap + 1:,}", marker)
        self.assertIn("browser.get_html", marker)

    async def test_a_raw_cached_screenshot_is_never_returned(self) -> None:
        """A read is a fresh, redacted, page-checked capture, whatever the session has cached.

        `last_screenshot` can be a raw capture (PII_SCRUB_ACTION_SCREENSHOTS=false with observation
        scrubbing on), and can be older than the page.
        """
        raw = Path(self.tempdir.name) / "raw.png"
        raw.write_bytes(b"RAW-CACHED-CANARY")
        self.browser_session.last_screenshot = {"path": str(raw), "url": "/artifacts/s1/raw.png"}
        fresh = Path(self.tempdir.name) / "fresh.png"
        fresh.write_bytes(b"FRESH-REDACTED")
        self.manager.capture_screenshot.return_value = {"screenshot_path": str(fresh)}

        answer = await self.read("browser://s1/screenshot")

        served = base64.standard_b64decode(answer["result"]["contents"][0]["blob"])
        self.assertEqual(served, b"FRESH-REDACTED")
        self.assertNotIn(b"CANARY", served)
        self.manager.capture_screenshot.assert_awaited_once_with("s1", label="mcp-resource")


if __name__ == "__main__":
    unittest.main()
