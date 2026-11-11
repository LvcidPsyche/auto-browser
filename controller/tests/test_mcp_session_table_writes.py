"""One MCP initialize cannot stop every later one.

The transport keeps each session's clientInfo and capabilities and writes the whole table of sessions to one file, as
UTF-8, at every initialize. A lone surrogate in either (JSON's "\\ud800" reads as one) could not be encoded: the caller
was answered 500, the session stayed in the table, and so every later write of the table failed the same way. Each
initialize by anybody answered 500 and left one more session nobody held the id of, until a restart read the table
back from the file.

Both halves are held here: what a client sends is stored with U+FFFD for each lone surrogate, and a session whose
first write fails, for whatever reason, is taken back out of the table before the error goes on.

What a client sends is bounded too, by two numbers of the controller's own. The table is written whole at every
write of it, so a clientInfo nested some hundreds of objects deep, which an initialize's write went down and a
DELETE's write (made from a shallower place in the stack) did not, made every client's DELETE answer 500, and one of
32 MiB was written again at every write by anybody.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import mcp_transport
from app.mcp_transport import (
    MAX_CLIENT_OBJECT_DEPTH,
    MAX_CLIENT_OBJECT_JSON_BYTES,
    MCP_PROTOCOL_HEADER,
    MCP_SESSION_HEADER,
    McpHttpTransport,
)

PROTOCOL = "2025-11-25"
LONE = "\ud800"
# As a client that decoded its own name with surrogateescape sends it: in a value, in a key and below the top level.
CLIENT_INFO = {"name": "client" + LONE, "version": "1", "build" + LONE: {"tags": ["a" + LONE]}}
CAPABILITIES = {"roots": {"listChanged": True}, "experimental": {"x" + LONE: "y" + LONE}}
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


def _objects(depth: int) -> str:
    """The JSON of ``depth`` objects inside one another, the outermost counted."""
    return '{"a":' * (depth - 1) + "{}" + "}" * (depth - 1)


def _lists_in_an_object(depth: int) -> str:
    """The JSON of an object that holds lists inside one another, ``depth`` levels with the object."""
    return '{"a":' + "[" * (depth - 1) + "]" * (depth - 1) + "}"


def _of_bytes(size: int, *, letter: str = "x") -> str:
    """The JSON of an object that takes ``size`` bytes written compactly as UTF-8, its text made of ``letter``."""
    room = size - len('{"name":""}')
    each = len(letter.encode("utf-8"))
    value = {"name": letter * (room // each) + "x" * (room % each)}
    assert len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) == size
    # ASCII on the wire, so more bytes there than are counted: what is counted is what the controller would write.
    return json.dumps(value)


TOO_DEEP = f"is nested deeper than {MAX_CLIENT_OBJECT_DEPTH} levels"
TOO_LARGE = f"is larger than {MAX_CLIENT_OBJECT_JSON_BYTES} bytes as JSON"
# What is kept at each bound, what is refused one past it, and the words of the refusal.
AT_AND_PAST_A_BOUND = {
    "objects in one another": (_objects(MAX_CLIENT_OBJECT_DEPTH), _objects(MAX_CLIENT_OBJECT_DEPTH + 1), TOO_DEEP),
    "lists in one another": (
        _lists_in_an_object(MAX_CLIENT_OBJECT_DEPTH),
        _lists_in_an_object(MAX_CLIENT_OBJECT_DEPTH + 1),
        TOO_DEEP,
    ),
    "bytes of JSON": (
        _of_bytes(MAX_CLIENT_OBJECT_JSON_BYTES),
        _of_bytes(MAX_CLIENT_OBJECT_JSON_BYTES + 1),
        TOO_LARGE,
    ),
    "bytes of JSON, of text that takes two bytes a letter": (
        _of_bytes(MAX_CLIENT_OBJECT_JSON_BYTES, letter=chr(0xE9)),
        _of_bytes(MAX_CLIENT_OBJECT_JSON_BYTES + 1, letter=chr(0xE9)),
        TOO_LARGE,
    ),
}


class _Transport(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = Path(self.tempdir.name) / "mcp-sessions.json"
        self.transport = self.transport_over_the_store()
        app = FastAPI()

        @app.post("/mcp")
        async def post_mcp(request: Request):
            return await self.transport.handle_post_request(request)

        @app.delete("/mcp")
        async def delete_mcp(request: Request):
            return await self.transport.handle_delete_request(request)

        # A failed write is answered 500 by the server, as the shipped app answers it.
        self.client = TestClient(app, raise_server_exceptions=False)

    def transport_over_the_store(self) -> McpHttpTransport:
        return McpHttpTransport(
            tool_gateway=SimpleNamespace(list_tools=lambda: []),
            server_name="auto-browser",
            server_title="Auto Browser MCP",
            server_version="0.2.0",
            allowed_origins=[],
            session_store_path=str(self.store),
            manager=SimpleNamespace(sessions={}),
        )

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

    def initialize_as_written(self, *, client_info: str = "{}", capabilities: str = "{}"):
        """An initialize whose clientInfo and capabilities are the JSON given, byte for byte."""
        params = f'{{"protocolVersion":"{PROTOCOL}","clientInfo":{client_info},"capabilities":{capabilities}}}'
        body = f'{{"jsonrpc":"2.0","id":1,"method":"initialize","params":{params}}}'
        return self.client.post("/mcp", content=body.encode("ascii"), headers={"content-type": "application/json"})

    def delete(self, session_id: str):
        return self.client.delete("/mcp", headers={MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: PROTOCOL})

    def stored(self) -> list[dict]:
        return json.loads(self.store.read_text(encoding="utf-8"))

    def assert_an_ordinary_answer(self, response) -> str:
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["protocolVersion"], PROTOCOL)
        self.assertEqual(response.headers[MCP_PROTOCOL_HEADER], PROTOCOL)
        return response.headers[MCP_SESSION_HEADER]


class McpSessionTableWriteTests(_Transport):
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

    def test_at_the_cap_a_session_whose_first_write_fails_puts_no_other_session_out(self) -> None:
        # Room was made for the new session before the write that failed: the least recently used session was gone
        # from memory and its stream ended, though the file still held it and a restart brought it back.
        with patch.object(mcp_transport, "MAX_MCP_SESSIONS", 3):
            kept = [self.assert_an_ordinary_answer(self.initialize()) for _ in range(3)]
            for second, session_id in enumerate(kept):
                self.transport._sessions[session_id].last_used_at = f"2026-11-01T00:00:0{second}Z"
            streams = [self.transport._take_over_event_stream(session_id) for session_id in kept]
            table_before = self.store.read_text(encoding="utf-8")

            with patch.object(mcp_transport, "atomic_write_text", side_effect=OSError(28, "No space left on device")):
                refused = self.initialize()

            self.assertEqual(refused.status_code, 500)
            self.assertEqual(list(self.transport._sessions), kept)
            self.assertEqual([stop.is_set() for stop in streams], [False, False, False])
            self.assertEqual(self.store.read_text(encoding="utf-8"), table_before)

            # An initialize that is written still puts the least recently used out, of memory and of the file.
            after = self.assert_an_ordinary_answer(self.initialize())
            self.assertEqual(list(self.transport._sessions), [*kept[1:], after])
            self.assertEqual([stop.is_set() for stop in streams], [True, False, False])
            self.assertEqual([record["id"] for record in self.stored()], [*kept[1:], after])


class McpClientObjectBoundsTests(_Transport):
    """clientInfo and capabilities are each kept up to two bounds of the controller's own, and refused past either."""

    SENT_AS = {"clientInfo": "client_info", "capabilities": "capabilities"}
    STORED_AS = {"clientInfo": "client_info", "capabilities": "client_capabilities"}

    def test_each_is_kept_at_a_bound_and_refused_one_past_it(self) -> None:
        for field, argument in self.SENT_AS.items():
            for bound, (at, past, words) in AT_AND_PAST_A_BOUND.items():
                with self.subTest(field=field, bound=bound):
                    kept = self.assert_an_ordinary_answer(self.initialize_as_written(**{argument: at}))
                    self.assertEqual(self.stored()[-1]["id"], kept)
                    self.assertEqual(self.stored()[-1][self.STORED_AS[field]], json.loads(at))
                    table, sessions = self.store.read_bytes(), sorted(self.transport._sessions)

                    refused = self.initialize_as_written(**{argument: past})

                    # A JSON-RPC error, as the transport answers params it does not take. It names the field and
                    # the bound, and nothing of what was sent.
                    self.assertEqual(refused.status_code, 200, refused.text[:300])
                    error = {"code": -32602, "message": f"initialize: {field} {words}"}
                    self.assertEqual(refused.json(), {"jsonrpc": "2.0", "id": 1, "error": error})
                    self.assertNotIn(MCP_SESSION_HEADER, refused.headers)
                    # No session, and no write: the table is what it was, in memory and in the file.
                    self.assertEqual(sorted(self.transport._sessions), sessions)
                    self.assertEqual(self.store.read_bytes(), table)

    def test_a_session_at_both_bounds_stops_no_other_clients_delete(self) -> None:
        # The 500 was of every DELETE by anybody, while one session nested 486 to 490 deep (Python 3.12) was in the
        # table: its initialize had written it, and a DELETE writes the same table from fewer frames up.
        at_the_bounds = self.assert_an_ordinary_answer(
            self.initialize_as_written(
                client_info=_of_bytes(MAX_CLIENT_OBJECT_JSON_BYTES), capabilities=_objects(MAX_CLIENT_OBJECT_DEPTH)
            )
        )
        another = self.assert_an_ordinary_answer(self.initialize())

        deleted = self.delete(another)

        self.assertEqual(deleted.status_code, 204, deleted.text[:300])
        self.assertEqual(list(self.transport._sessions), [at_the_bounds])
        self.assertEqual([record["id"] for record in self.stored()], [at_the_bounds])
        self.assertEqual(self.delete(at_the_bounds).status_code, 204)
        self.assertEqual(self.stored(), [])

    def test_a_table_an_older_build_wrote_is_read_without_the_sessions_past_a_bound(self) -> None:
        # An older build kept whatever its writer could go down (488 objects on Python 3.11) and any size.
        marker = "VALUE-OF-THE-CLIENT"
        deep = json.loads(_objects(200).replace("{}", json.dumps({"k": marker})))
        large = {"name": marker + "x" * MAX_CLIENT_OBJECT_JSON_BYTES}

        def record(session_id: str, client_info: dict, capabilities: dict) -> dict:
            return {
                "id": session_id,
                "protocol_version": PROTOCOL,
                "client_info": client_info,
                "client_capabilities": capabilities,
                "initialized": True,
                "created_at": "2026-11-01T00:00:00+00:00",
                "last_used_at": "2026-11-01T00:00:00+00:00",
                "resource_subscriptions": [],
                "owner": None,
            }

        written = [
            record("deep-info", deep, {}),
            record("plain", {"name": "pytest", "version": "1"}, {"roots": {"listChanged": True}}),
            record("deep-capabilities", {"name": "pytest"}, deep),
            record("large-info", large, {}),
            record("large-capabilities", {}, large),
            record("at-the-bounds", json.loads(_objects(MAX_CLIENT_OBJECT_DEPTH)), {}),
        ]
        self.store.write_text(json.dumps(written), encoding="utf-8")

        with self.assertLogs("app.mcp_transport", level="WARNING") as logs:
            self.transport = self.transport_over_the_store()

        self.assertEqual(list(self.transport._sessions), ["plain", "at-the-bounds"])
        (line,) = logs.output  # one line for the four, and nothing of what they held
        self.assertIn("4 MCP sessions", line)
        self.assertNotIn(marker, line)
        for name in ("deep-info", "deep-capabilities", "large-info", "large-capabilities"):
            self.assertNotIn(name, line)
            self.assertEqual(self.delete(name).status_code, 404)
        # The sessions that are kept go on, and the next write of the table leaves the others out of the file too.
        self.assertEqual(self.delete("plain").status_code, 204)
        self.assertEqual([item["id"] for item in self.stored()], ["at-the-bounds"])

    def test_a_body_the_json_reader_cannot_go_down_is_answered_as_one_that_cannot_be_read(self) -> None:
        # How deep Python's JSON reader goes before it gives up (RecursionError, which is no ValueError) is the
        # interpreter's own, so the reader is stood in for: such a body was an unhandled 500.
        with patch.object(Request, "json", AsyncMock(side_effect=RecursionError("maximum recursion depth exceeded"))):
            refused = self.initialize()

        self.assertEqual(refused.status_code, 400, refused.text[:300])
        self.assertEqual(refused.json()["error"], {"code": -32700, "message": "Invalid JSON payload"})
        self.assertEqual(self.transport._sessions, {})


if __name__ == "__main__":
    unittest.main()
