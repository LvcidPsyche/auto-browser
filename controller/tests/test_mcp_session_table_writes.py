"""One MCP initialize cannot stop every later one.

The transport keeps each session's clientInfo and capabilities and writes the whole table of sessions to one file, as
UTF-8, at every initialize. A lone surrogate in either (JSON's "\\ud800" reads as one) could not be encoded: the caller
was answered 500, the session stayed in the table, and so every later write of the table failed the same way. Each
initialize by anybody answered 500 and left one more session nobody held the id of, until a restart read the table
back from the file.

Both halves are held here: what a client sends is stored with U+FFFD for each lone surrogate, and a session whose
first write fails, for whatever reason, is taken back out of the table before the error goes on.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import mcp_transport
from app.mcp_transport import MCP_PROTOCOL_HEADER, MCP_SESSION_HEADER, McpHttpTransport

PROTOCOL = "2025-11-25"
LONE = "\ud800"
# As a client that decoded its own name with surrogateescape sends it: in a value, in a key and below the top level.
CLIENT_INFO = {"name": "client" + LONE, "version": "1", "build" + LONE: {"tags": ["a" + LONE]}}
CAPABILITIES = {"roots": {"listChanged": True}, "experimental": {"x" + LONE: "y" + LONE}}
# Objects inside one another: more than the table's writer goes down (it takes two frames a level, of the 1,000 the
# interpreter allows by default), fewer than the request's parser refuses.
TOO_DEEP_TO_WRITE = 600
REPLACEMENT = chr(0xFFFD)  # U+FFFD, by its number: the character itself in this file would read as a mistake


def _with(value: object, old: str, new: str) -> object:
    """``value`` with ``old`` replaced by ``new`` in every string of it, keys included."""
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, dict):
        return {_with(key, old, new): _with(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_with(item, old, new) for item in value]
    return value


class McpSessionTableWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = Path(self.tempdir.name) / "mcp-sessions.json"
        self.transport = McpHttpTransport(
            tool_gateway=SimpleNamespace(list_tools=lambda: []),
            server_name="auto-browser",
            server_title="Auto Browser MCP",
            server_version="0.2.0",
            allowed_origins=[],
            session_store_path=str(self.store),
            manager=SimpleNamespace(sessions={}),
        )
        app = FastAPI()

        @app.post("/mcp")
        async def post_mcp(request: Request):
            return await self.transport.handle_post_request(request)

        # A failed write is answered 500 by the server, as the shipped app answers it.
        self.client = TestClient(app, raise_server_exceptions=False)

    def initialize(self, client_info: dict | None = None, capabilities: dict | None = None):
        params = {
            "protocolVersion": PROTOCOL,
            "clientInfo": {"name": "pytest", "version": "1"} if client_info is None else client_info,
            "capabilities": capabilities or {},
        }
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
        # ASCII JSON, where a lone surrogate is the escape a client would send; it could not be sent as UTF-8.
        return self.client.post(
            "/mcp", content=json.dumps(body).encode("ascii"), headers={"content-type": "application/json"}
        )

    def stored(self) -> list[dict]:
        return json.loads(self.store.read_text(encoding="utf-8"))

    def assert_an_ordinary_answer(self, response) -> str:
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["protocolVersion"], PROTOCOL)
        self.assertEqual(response.headers[MCP_PROTOCOL_HEADER], PROTOCOL)
        return response.headers[MCP_SESSION_HEADER]

    def test_an_initialize_holding_a_lone_surrogate_is_answered_and_stops_no_later_one(self) -> None:
        first = self.assert_an_ordinary_answer(self.initialize())

        second = self.assert_an_ordinary_answer(self.initialize(CLIENT_INFO, CAPABILITIES))

        third = self.assert_an_ordinary_answer(self.initialize())
        fourth = self.assert_an_ordinary_answer(self.initialize())
        self.assertEqual([record["id"] for record in self.stored()], [first, second, third, fourth])
        self.assertEqual(sorted(self.transport._sessions), sorted([first, second, third, fourth]))

    def test_what_the_client_sent_is_stored_with_the_replacement_character(self) -> None:
        session_id = self.assert_an_ordinary_answer(self.initialize(CLIENT_INFO, CAPABILITIES))

        (record,) = self.stored()
        self.assertEqual(record["id"], session_id)
        self.assertEqual(record["client_info"], _with(CLIENT_INFO, LONE, REPLACEMENT))
        self.assertEqual(record["client_capabilities"], _with(CAPABILITIES, LONE, REPLACEMENT))
        self.assertEqual(record["client_info"]["name"], "client" + REPLACEMENT)
        self.assertEqual(self.store.read_text(encoding="utf-8").count(REPLACEMENT), 5)

    def test_the_session_of_such_an_initialize_is_one_like_any_other(self) -> None:
        session_id = self.assert_an_ordinary_answer(self.initialize(CLIENT_INFO, CAPABILITIES))
        headers = {MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: PROTOCOL}

        # A second write of the table, which holds that session.
        ready = self.client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )
        listed = self.client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )

        self.assertEqual((ready.status_code, listed.status_code), (202, 200), (ready.text, listed.text))
        self.assertEqual([record["initialized"] for record in self.stored()], [True])

    def test_a_session_whose_first_write_fails_is_not_kept(self) -> None:
        kept = self.assert_an_ordinary_answer(self.initialize())
        table_before = self.store.read_text(encoding="utf-8")

        with patch.object(mcp_transport, "atomic_write_text", side_effect=OSError(28, "No space left on device")):
            refused = self.initialize()

        self.assertEqual(refused.status_code, 500)
        self.assertNotIn(MCP_SESSION_HEADER, refused.headers)
        # No session nobody holds the id of: the table is what it was, in memory and in the file.
        self.assertEqual(list(self.transport._sessions), [kept])
        self.assertEqual(self.store.read_text(encoding="utf-8"), table_before)

        after = self.assert_an_ordinary_answer(self.initialize())
        self.assertEqual([record["id"] for record in self.stored()], [kept, after])

    def test_capabilities_nested_deeper_than_the_writer_goes_cost_their_own_initialize_alone(self) -> None:
        # No mock: the table's writer gives up (RecursionError) on a depth the request's parser takes, so this
        # session could not be written either, and it stopped every later initialize as the lone surrogate did.
        kept = self.assert_an_ordinary_answer(self.initialize())
        deep = '{"a":' * TOO_DEEP_TO_WRITE + "1" + "}" * TOO_DEEP_TO_WRITE
        body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"%s","capabilities":%s}}'

        refused = self.client.post(
            "/mcp", content=(body % (PROTOCOL, deep)).encode("ascii"), headers={"content-type": "application/json"}
        )

        self.assertGreaterEqual(refused.status_code, 400, refused.text[:200])
        self.assertNotIn(MCP_SESSION_HEADER, refused.headers)
        self.assertEqual(list(self.transport._sessions), [kept])
        after = self.assert_an_ordinary_answer(self.initialize())
        self.assertEqual([record["id"] for record in self.stored()], [kept, after])


if __name__ == "__main__":
    unittest.main()
