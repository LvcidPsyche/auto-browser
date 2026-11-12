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

import gc
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import mcp_transport
from app.mcp_transport import MCP_PROTOCOL_HEADER, MCP_SESSION_HEADER, McpHttpTransport
from app.utils import without_lone_surrogates

PROTOCOL = "2025-11-25"
LONE = "\ud800"
# As a client that decoded its own name with surrogateescape sends it: in a value, in a key and below the top level.
CLIENT_INFO = {"name": "client" + LONE, "version": "1", "build" + LONE: {"tags": ["a" + LONE]}}
CAPABILITIES = {"roots": {"listChanged": True}, "experimental": {"x" + LONE: "y" + LONE}}
REPLACEMENT = chr(0xFFFD)  # U+FFFD, by its number: the character itself in this file would read as a mistake
# The bounds the CHANGELOG and docs/mcp-clients.md promise, written out: rows built from the module's two constants
# held that each bound is exact at whatever the constant is, and passed with it at 33 or at 65,537.
DEPTH_BOUND = 32
BYTES_BOUND = 65536


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


def _compact_size(value: object) -> int:
    """The bytes ``value`` takes written compactly as JSON in UTF-8, a lone surrogate as the three U+FFFD takes."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass"))


# 2,000 keys of one lone surrogate each: once each is replaced by U+FFFD they meet, and all but the first are numbered.
MEETING_KEYS = {chr(0xD800 + number): 0 for number in range(2000)}


def _padded(value: dict, size: int, *, as_kept: bool) -> dict:
    """``value`` and a key of x's that make it ``size`` bytes as sent, or as kept (its lone surrogates replaced)."""

    def measured(padded: dict) -> int:
        return _compact_size(without_lone_surrogates(padded) if as_kept else padded)

    padded = {**value, "pad": ""}
    padded["pad"] = "x" * (size - measured(padded))
    assert measured(padded) == size
    return padded


def _record(session_id: str, client_info: dict, capabilities: dict) -> dict:
    """A session's record as the table holds it."""
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


TOO_DEEP = f"is nested deeper than {DEPTH_BOUND} levels"
TOO_LARGE = f"is larger than {BYTES_BOUND} bytes as JSON"
# What is kept at each bound, what is refused one past it, and the words of the refusal.
AT_AND_PAST_A_BOUND = {
    "objects in one another": (_objects(DEPTH_BOUND), _objects(DEPTH_BOUND + 1), TOO_DEEP),
    "lists in one another": (
        _lists_in_an_object(DEPTH_BOUND),
        _lists_in_an_object(DEPTH_BOUND + 1),
        TOO_DEEP,
    ),
    "bytes of JSON": (
        _of_bytes(BYTES_BOUND),
        _of_bytes(BYTES_BOUND + 1),
        TOO_LARGE,
    ),
    "bytes of JSON, of text that takes two bytes a letter": (
        _of_bytes(BYTES_BOUND, letter=chr(0xE9)),
        _of_bytes(BYTES_BOUND + 1, letter=chr(0xE9)),
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
            self.initialize_as_written(client_info=_of_bytes(BYTES_BOUND), capabilities=_objects(DEPTH_BOUND))
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
        large = {"name": marker + "x" * BYTES_BOUND}
        written = [
            _record("deep-info", deep, {}),
            _record("plain", {"name": "pytest", "version": "1"}, {"roots": {"listChanged": True}}),
            _record("deep-capabilities", {"name": "pytest"}, deep),
            _record("large-info", large, {}),
            _record("large-capabilities", {}, large),
            _record("at-the-bounds", json.loads(_objects(DEPTH_BOUND)), {}),
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
        # The sessions that are kept go on, and the start has left the others out of the file too.
        self.assertEqual([item["id"] for item in self.stored()], ["plain", "at-the-bounds"])
        self.assertEqual(self.delete("plain").status_code, 204)
        self.assertEqual([item["id"] for item in self.stored()], ["at-the-bounds"])

    def test_what_is_kept_is_measured_and_not_only_what_was_sent(self) -> None:
        # Keys that meet once their lone surrogates are replaced are numbered (" (2)", " (3)", ...), so what is kept
        # is longer than what was sent: a value at the bound as sent was kept past it, written at every write of the
        # table, and dropped by the next start, which measures what was kept.
        at_the_bound_as_sent = _padded(MEETING_KEYS, BYTES_BOUND, as_kept=False)
        self.assertGreater(_compact_size(without_lone_surrogates(at_the_bound_as_sent)), BYTES_BOUND)
        kept = self.assert_an_ordinary_answer(self.initialize())
        table = self.store.read_bytes()

        for field, argument in self.SENT_AS.items():
            with self.subTest(field=field):
                refused = self.initialize(**{argument: at_the_bound_as_sent})

                self.assertEqual(refused.status_code, 200, refused.text[:300])
                error = {"code": -32602, "message": f"initialize: {field} {TOO_LARGE}"}
                self.assertEqual(refused.json(), {"jsonrpc": "2.0", "id": 1, "error": error})
                self.assertNotIn(MCP_SESSION_HEADER, refused.headers)
                self.assertEqual(list(self.transport._sessions), [kept])
                self.assertEqual(self.store.read_bytes(), table)

    def test_a_value_at_the_bound_as_kept_is_kept_and_read_back_at_the_next_start(self) -> None:
        at_the_bound_as_kept = _padded(MEETING_KEYS, BYTES_BOUND, as_kept=True)
        made = {
            argument: self.assert_an_ordinary_answer(self.initialize(**{argument: at_the_bound_as_kept}))
            for argument in self.SENT_AS.values()
        }
        stored = {record["id"]: record for record in self.stored()}
        self.assertEqual(stored[made["client_info"]]["client_info"], without_lone_surrogates(at_the_bound_as_kept))
        self.assertEqual(
            stored[made["capabilities"]]["client_capabilities"], without_lone_surrogates(at_the_bound_as_kept)
        )

        with self.assertNoLogs("app.mcp_transport", level="WARNING"):
            self.transport = self.transport_over_the_store()

        self.assertEqual(sorted(self.transport._sessions), sorted(made.values()))

    def test_a_value_of_large_integers_is_refused_by_the_walk_without_being_written_out(self) -> None:
        # The walk counted an integer as one byte, so a value of a few large ones passed it and was written out whole
        # to be refused: 2,000 integers of 4,000 digits wrote 8 MB, on the event loop.
        large = {"a": [int("9" * 4000)] * 20}

        with patch.object(mcp_transport.json, "dumps", wraps=json.dumps) as dumps:
            verdict, _ = mcp_transport._past_a_client_object_bound(large)

        self.assertEqual(verdict, TOO_LARGE)
        self.assertEqual(dumps.call_count, 0)
        refused = self.initialize_as_written(capabilities=json.dumps(large))
        error = {"code": -32602, "message": f"initialize: capabilities {TOO_LARGE}"}
        self.assertEqual(refused.json(), {"jsonrpc": "2.0", "id": 1, "error": error})
        self.assertEqual(self.transport._sessions, {})

    def test_a_value_of_small_integers_is_still_measured_and_kept_at_the_bound(self) -> None:
        integers = {"a": [99999, -12345, 0, 7] * 3500}
        at = _padded(integers, BYTES_BOUND, as_kept=False)
        past = _padded(integers, BYTES_BOUND + 1, as_kept=False)

        for value, verdict in ((at, None), (past, TOO_LARGE)):
            with patch.object(mcp_transport.json, "dumps", wraps=json.dumps) as dumps:
                self.assertEqual(mcp_transport._past_a_client_object_bound(value)[0], verdict)
            # Written out to be measured: the walk counted no more than the JSON takes.
            self.assertEqual(dumps.call_count, 1)
        self.assert_an_ordinary_answer(self.initialize_as_written(capabilities=json.dumps(at)))
        self.assertEqual(self.stored()[-1]["client_capabilities"], at)

    def test_an_integer_is_counted_at_no_more_than_its_json_takes(self) -> None:
        # From its bits, which costs nothing: str() of it costs what writing it out does. A bool is an int. Each
        # length, near the ends, and a sample between them: what is counted falls behind as the integer grows.
        numbers = [0, True, False]
        for digits in [*range(1, 100), *range(100, 3950, 37), *range(3950, 4001)]:
            numbers += [10 ** (digits - 1), 10**digits - 1, -(10**digits - 1)]
        for bits in [*range(1, 400), *range(400, 13_100, 101), *range(13_100, 13_280)]:
            numbers += [2**bits, 2**bits - 1, -(2**bits)]
        for number in numbers:
            written = len(json.dumps(number))
            counted = mcp_transport._json_length_at_least(number)
            if counted > written or (not isinstance(number, bool) and counted < written - 2):
                self.fail(f"an integer of {written} characters as JSON is counted {counted}")

    def test_the_table_is_written_as_the_bounds_measure_it_compactly(self) -> None:
        # Written indented, a session at the bounds took some 35 times what is bounded: fields of lists 32 deep at
        # 64 KiB compact were 4.44 MiB a session in the table, 2.17 GiB at its cap of 500.
        value = {"a": [[[0, 1.5, "x"]]], "b": {"c": None}}
        self.assert_an_ordinary_answer(self.initialize(client_info=value, capabilities=value))

        text = self.store.read_text(encoding="utf-8")
        self.assertNotIn("\n", text)
        self.assertEqual(text, json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":")))
        (session,) = self.stored()
        self.assertEqual((session["client_info"], session["client_capabilities"]), (value, value))
        # Every controller reads it back as it reads one written indented.
        self.assertEqual(list(self.transport_over_the_store()._sessions), [session["id"]])

    def test_a_value_of_floats_past_the_bound_is_refused_by_the_walk_without_being_written_out(self) -> None:
        # The walk counted a float as one byte: 65,000 floats were counted 65,000 bytes, and written out (1,625,007
        # bytes) to be refused.
        floats = {"a": [-2.2250738585072014e-308] * 3000}

        with patch.object(mcp_transport.json, "dumps", wraps=json.dumps) as dumps:
            verdict, _ = mcp_transport._past_a_client_object_bound(floats)

        self.assertEqual(verdict, TOO_LARGE)
        self.assertEqual(dumps.call_count, 0)
        refused = self.initialize_as_written(capabilities=json.dumps(floats))
        error = {"code": -32602, "message": f"initialize: capabilities {TOO_LARGE}"}
        self.assertEqual(refused.json(), {"jsonrpc": "2.0", "id": 1, "error": error})
        self.assertEqual(self.transport._sessions, {})

    def test_a_float_is_counted_at_the_length_it_is_written_at(self) -> None:
        # NaN and the infinities too: the JSON reader takes them from a body, and the writer writes them so.
        for number in (0.0, -0.0, 0.5, 1e16, 1e-7, -2.2250738585072014e-308, 1.7976931348623157e308, 1 / 3):
            with self.subTest(number=number):
                self.assertEqual(mcp_transport._json_length_at_least(number), len(json.dumps(number)))
        for number in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(number=number):
                self.assertEqual(mcp_transport._json_length_at_least(number), len(json.dumps(number)))

    def test_a_value_of_floats_and_non_finite_numbers_at_the_bound_is_kept(self) -> None:
        # Counted as written, so a value at the bound is still written out once to be measured, and kept.
        numbers = {"a": [0.25, float("nan"), float("-inf"), 1e300] * 2000}
        at = _padded(numbers, BYTES_BOUND, as_kept=False)

        with patch.object(mcp_transport.json, "dumps", wraps=json.dumps) as dumps:
            self.assertIsNone(mcp_transport._past_a_client_object_bound(at)[0])
        self.assertEqual(dumps.call_count, 1)

    def test_a_body_the_json_reader_cannot_go_down_is_answered_as_one_that_cannot_be_read(self) -> None:
        # How deep Python's JSON reader goes before it gives up (RecursionError, which is no ValueError) is the
        # interpreter's own, so the reader is stood in for: such a body was an unhandled 500.
        with patch.object(Request, "json", AsyncMock(side_effect=RecursionError("maximum recursion depth exceeded"))):
            refused = self.initialize()

        self.assertEqual(refused.status_code, 400, refused.text[:300])
        self.assertEqual(refused.json()["error"], {"code": -32700, "message": "Invalid JSON payload"})
        self.assertEqual(self.transport._sessions, {})


def _deep_and_wide(depth: int) -> dict:
    """An object whose lists go ``depth`` levels down with it, each level holding numbers and text of brackets."""
    value: list = [1, "]", "[{"]
    for _ in range(depth - 2):
        value = [value, 0, "}]", '\\"[', "\\"]
    return {"a": value}


def _start_cases() -> dict[str, dict]:
    """Values a start reads, on each side of each bound, in the shapes the two ways of measuring could tell apart."""
    integers = {"a": [99999, -12345, 0, 7] * 3500}
    numbers = {"a": [0.25, float("nan"), float("-inf"), 1e300] * 2000}
    surrogates = {"name" + LONE: LONE * 100, "b": ["x" + LONE]}
    sent = _padded(MEETING_KEYS, BYTES_BOUND, as_kept=False)
    cases = {"empty": {}, "objects 200 deep": json.loads(_objects(200))}
    for bound, (at, past, _) in AT_AND_PAST_A_BOUND.items():
        cases |= {f"{bound}, at": json.loads(at), f"{bound}, past": json.loads(past)}
    for side, size in (("at", BYTES_BOUND), ("past", BYTES_BOUND + 1)):
        cases |= {
            f"letters of four bytes, {side}": json.loads(_of_bytes(size, letter=chr(0x1F600))),
            f"lone surrogates, {side}": _padded(surrogates, size, as_kept=False),
            f"small numbers, {side}": _padded(integers, size, as_kept=False),
            f"floats and non-finite numbers, {side}": _padded(numbers, size, as_kept=False),
        }
    return cases | {
        "deep and wide, at": _deep_and_wide(DEPTH_BOUND),
        "deep and wide, past": _deep_and_wide(DEPTH_BOUND + 1),
        "text of brackets at the depth bound": json.loads(_objects(DEPTH_BOUND).replace("{}", '{"k":"[[{{\\\\"}')),
        "text of brackets and escapes": {"[[[[": "{{{{" * 40, "a\\": ["\\", {"b": '"[{'}], '"{': "\\]"},
        "empty lists, at": {"a": [[] for _ in range(21_843)]},
        "empty lists, past": {"a": [[] for _ in range(21_844)]},
        "large integers": {"a": [int("9" * 4000)] * 20},
        "keys that meet once cleaned, as sent": sent,
        "keys that meet once cleaned, as kept": without_lone_surrogates(sent),
        "keys that met, at the bound as kept": without_lone_surrogates(
            _padded(MEETING_KEYS, BYTES_BOUND, as_kept=True)
        ),
    }


class McpSessionTableStartTests(_Transport):
    """A start measures each stored clientInfo and capabilities by the text the table writes of it.

    The initialize's walk goes through a value element by element, in Python, so that it stops at the first thing
    past a bound in a body of any size. A start ran it on each stored field whole, before the controller answered
    anything: a table of 500 sessions with both fields of small numbers at the bound took some 40 s to read back, at
    every restart. What a start keeps and drops is still what an initialize keeps and refuses.
    """

    def test_a_start_goes_through_no_stored_field_element_by_element(self) -> None:
        records = [_record(f"s{number}", {"a": [0] * 1000}, {"b": [1.5, "x"] * 500}) for number in range(3)]
        self.store.write_text(json.dumps(records), encoding="utf-8")

        with (
            patch.object(
                mcp_transport, "_past_a_client_object_bound", wraps=mcp_transport._past_a_client_object_bound
            ) as walks,
            patch.object(mcp_transport, "_json_length_at_least", wraps=mcp_transport._json_length_at_least) as counted,
            patch.object(mcp_transport.json, "dumps", wraps=json.dumps) as dumps,
        ):
            self.transport = self.transport_over_the_store()

        self.assertEqual(sorted(self.transport._sessions), ["s0", "s1", "s2"])
        # No walk, so no element counted; each field written out once, which is the text each write of the table joins.
        self.assertEqual((walks.call_count, counted.call_count), (0, 0))
        self.assertEqual(dumps.call_count, 6)

    def as_an_initialize_keeps(self, value: dict) -> str | None:
        """The text an initialize keeps of ``value`` sent as its clientInfo, or None where it refuses it."""
        answer = self.initialize(client_info=value)
        self.assertEqual(answer.status_code, 200, answer.text[:300])
        if "error" in answer.json():
            return None
        return self.transport._sessions[answer.headers[MCP_SESSION_HEADER]].client_json[0]

    def test_a_start_keeps_what_an_initialize_keeps_and_drops_what_it_refuses(self) -> None:
        cases = _start_cases()
        # As an initialize decides of each: None, or the text it keeps, its lone surrogates as U+FFFD.
        walked = {name: self.as_an_initialize_keeps(value) for name, value in cases.items()}
        kept = {name for name, text in walked.items() if text is not None}
        self.assertTrue(0 < len(kept) < len(cases), kept)
        records = []
        for name, value in cases.items():
            records += [_record(f"info: {name}", value, {}), _record(f"capabilities: {name}", {}, value)]
        # ASCII JSON, so a lone surrogate is written as its escape, as only a file written by hand could hold one.
        self.store.write_text(json.dumps(records), encoding="utf-8")

        with self.assertLogs("app.mcp_transport", level="WARNING") as logs:
            self.transport = self.transport_over_the_store()

        restored = self.transport._sessions
        expected = [f"{field}: {name}" for name in kept for field in ("info", "capabilities")]
        self.assertEqual(sorted(restored), sorted(expected))
        (line,) = logs.output
        self.assertIn(f"{2 * (len(cases) - len(kept))} MCP sessions", line)
        empty = mcp_transport._compact_json({})
        for name in kept:
            with self.subTest(name=name):
                self.assertEqual(restored[f"info: {name}"].client_json, (walked[name], empty))
                self.assertEqual(restored[f"capabilities: {name}"].client_json, (empty, walked[name]))

    def test_a_stored_field_the_writer_cannot_go_down_is_dropped_and_counted(self) -> None:
        # How deep the JSON writer goes is the interpreter's own, so it is stood in for: a field the reader went down
        # can be one the writer, called from further up the stack, cannot.
        encode = json.dumps

        def writer(value, *args, **kwargs) -> str:
            if isinstance(value, dict) and "past-the-writer" in value:
                raise RecursionError("maximum recursion depth exceeded while encoding a JSON object")
            return encode(value, *args, **kwargs)

        records = [_record("deep", {"past-the-writer": 1}, {}), _record("plain", {"name": "pytest"}, {})]
        self.store.write_text(json.dumps(records), encoding="utf-8")

        with (
            patch.object(mcp_transport.json, "dumps", side_effect=writer),
            self.assertLogs("app.mcp_transport", level="WARNING") as logs,
        ):
            self.transport = self.transport_over_the_store()

        self.assertEqual(list(self.transport._sessions), ["plain"])
        (line,) = logs.output
        self.assertIn("1 MCP sessions", line)

    def test_a_stored_field_holding_a_lone_surrogate_is_kept_as_an_initialize_keeps_it(self) -> None:
        # Kept as it was read, it failed every later write of the table, which is written as UTF-8: each initialize
        # by anybody answered 500. Only a table written by hand holds one, as every version has written it as UTF-8.
        self.store.write_text(json.dumps([_record("by-hand", CLIENT_INFO, CAPABILITIES)]), encoding="utf-8")
        self.assertIn('"client\\ud800"', self.store.read_text(encoding="utf-8"))

        self.transport = self.transport_over_the_store()

        session = self.transport._sessions["by-hand"]
        kept = (_with(CLIENT_INFO, LONE, REPLACEMENT), _with(CAPABILITIES, LONE, REPLACEMENT))
        self.assertEqual((session.client_info, session.client_capabilities), kept)
        self.assertEqual(session.client_json, tuple(mcp_transport._compact_json(value) for value in kept))
        headers = {MCP_SESSION_HEADER: "by-hand", MCP_PROTOCOL_HEADER: PROTOCOL}
        listed = self.client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        self.assertEqual(listed.json(), {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
        made = self.assert_an_ordinary_answer(self.initialize())
        self.assertEqual([record["id"] for record in self.stored()], ["by-hand", made])
        (record, _) = self.stored()
        self.assertEqual((record["client_info"], record["client_capabilities"]), kept)

    def test_a_record_whose_other_fields_hold_a_lone_surrogate_is_passed_over(self) -> None:
        # No version wrote one there, nor can an initialize make one: kept, it failed every later write of the table
        # as a client field's did.
        held = {
            "id": "by-hand" + LONE,
            "protocol_version": PROTOCOL + LONE,
            "created_at": "2026-11-01" + LONE,
            "last_used_at": "2026-11-01" + LONE,
            "owner": "operator" + LONE,
            "resource_subscriptions": ["browser://sessions" + LONE],
        }
        records = [{**_record(f"in {name}", {}, {}), name: value} for name, value in held.items()]
        self.store.write_text(json.dumps([*records, _record("plain", {}, {})]), encoding="utf-8")

        self.transport = self.transport_over_the_store()

        self.assertEqual(list(self.transport._sessions), ["plain"])
        made = self.assert_an_ordinary_answer(self.initialize())
        self.assertEqual([record["id"] for record in self.stored()], ["plain", made])

    PAST_AND_WITHIN = [_record("past", json.loads(_objects(DEPTH_BOUND + 1)), {}), _record("within", {"name": "a"}, {})]

    def test_a_start_that_drops_a_session_writes_the_table_without_it_once(self) -> None:
        # Dropped from memory only, it was read and measured again at every start, and one larger than the bound was
        # written out whole each time to be refused.
        self.store.write_text(json.dumps(self.PAST_AND_WITHIN), encoding="utf-8")

        with self.assertLogs("app.mcp_transport", level="WARNING") as logs:
            self.transport = self.transport_over_the_store()

        (line,) = logs.output
        self.assertIn("1 MCP sessions", line)
        self.assertEqual(self.stored(), [json.loads(json.dumps(asdict(self.transport._sessions["within"])))])
        # A start that drops none writes nothing.
        with (
            patch.object(mcp_transport, "atomic_write_text", wraps=mcp_transport.atomic_write_text) as writes,
            self.assertNoLogs("app.mcp_transport", level="WARNING"),
        ):
            self.transport = self.transport_over_the_store()
        self.assertEqual((writes.call_count, list(self.transport._sessions)), (0, ["within"]))

    def test_a_start_whose_write_back_fails_goes_on_and_serves_what_it_kept(self) -> None:
        self.store.write_text(json.dumps(self.PAST_AND_WITHIN), encoding="utf-8")
        table = self.store.read_bytes()

        with (
            patch.object(mcp_transport, "atomic_write_text", side_effect=OSError(28, "No space left on device")),
            self.assertLogs("app.mcp_transport", level="WARNING") as logs,
        ):
            self.transport = self.transport_over_the_store()

        dropped, failed = logs.output
        self.assertIn("1 MCP sessions", dropped)
        # One line, the error's own text: the table stays as it was, and the next start drops it again.
        self.assertIn("No space left on device", failed)
        self.assertNotIn("\n", failed)
        self.assertEqual(self.store.read_bytes(), table)
        self.assertEqual(list(self.transport._sessions), ["within"])
        headers = {MCP_SESSION_HEADER: "within", MCP_PROTOCOL_HEADER: PROTOCOL}
        listed = self.client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        self.assertEqual(listed.json(), {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
        with self.assertLogs("app.mcp_transport", level="WARNING") as logs:
            self.transport = self.transport_over_the_store()
        (line,) = logs.output
        self.assertIn("1 MCP sessions", line)
        self.assertEqual([record["id"] for record in self.stored()], ["within"])


class McpSessionTableStartCollectorTests(_Transport):
    """A start reads and measures the table with the cyclic garbage collector off, and gives it back as it found it.

    The read makes a list or an object for each one in the file, tens of millions at the table's cap, and no cycle:
    each collection it set off went through all of them, which none could free. On Python 3.11, the image's, a table
    of 500 sessions whose fields each held 1,000 lists nested 30 deep, side by side, was read in some 60 s with the
    collector on and in 3.4 s with it off.
    """

    TABLE = [_record("s0", {"a": [0]}, {"b": [[]]})]

    def setUp(self) -> None:
        super().setUp()
        collecting = gc.isenabled()
        self.addCleanup(gc.enable if collecting else gc.disable)
        self.store.write_text(json.dumps(self.TABLE), encoding="utf-8")

    def start_noting_the_collector(self, failing: str | None) -> list[tuple[str, bool]]:
        """A start whose read and measures note whether the collector is on, the step ``failing`` names raising."""
        read, measure = json.loads, mcp_transport._stored_client_json
        noted: list[tuple[str, bool]] = []

        def noting(step: str, call):
            def run(*args, **kwargs):
                noted.append((step, gc.isenabled()))
                if step == failing:
                    raise RuntimeError(f"{step} failed")
                return call(*args, **kwargs)

            return run

        with (
            patch.object(mcp_transport.json, "loads", side_effect=noting("the read", read)),
            patch.object(mcp_transport, "_stored_client_json", side_effect=noting("a measure", measure)),
        ):
            try:
                self.transport = self.transport_over_the_store()
            except RuntimeError as error:
                self.assertEqual(str(error), "a measure failed")
        return noted

    def test_the_read_and_the_measuring_run_with_the_collector_off(self) -> None:
        self.assertTrue(gc.isenabled())

        noted = self.start_noting_the_collector(None)

        self.assertEqual(noted, [("the read", False), ("a measure", False), ("a measure", False)])
        self.assertEqual(list(self.transport._sessions), ["s0"])
        self.assertTrue(gc.isenabled())

    def test_a_start_gives_the_collector_back_as_it_found_it_on_every_way_out(self) -> None:
        # The read that fails is logged and the start goes on with no session; a malformed record is passed over;
        # a measure that fails is the start's own error.
        malformed = [{"client_info": {}, "client_capabilities": {}}]
        for found_on in (True, False):
            for failing in (None, "the read", "a malformed record", "a measure"):
                with self.subTest(found_on=found_on, failing=failing):
                    table = malformed if failing == "a malformed record" else self.TABLE
                    self.store.write_text(json.dumps(table), encoding="utf-8")
                    (gc.enable if found_on else gc.disable)()

                    noted = self.start_noting_the_collector(failing)

                    self.assertEqual(gc.isenabled(), found_on)
                    self.assertTrue(noted)
                    self.assertEqual([on for _, on in noted], [False] * len(noted))
                    gc.enable()


NOTIFIED = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
SUBSCRIBED = {"jsonrpc": "2.0", "id": 3, "method": "resources/subscribe", "params": {"uri": "browser://sessions"}}


class McpSessionTableJoinTests(_Transport):
    """A write of the table copies and encodes no client's fields again.

    The table is written whole, on the event loop, at each initialize, notifications/initialized and DELETE /mcp. Each
    write copied every session (asdict) and encoded it whole: at the cap of 500 sessions, with both client fields of
    each at the bounds, one write took some 13 s. Each field is now encoded once, when its session is made, and a write
    joins that text with the session's other fields, encoded at the write.
    """

    INFO = {"name": "INFO-OF-THE-CLIENT", "version": "1"}
    CAPABILITIES = {"experimental": {"x": ["CAPABILITIES-OF-THE-CLIENT", 1.5, None, chr(0xE9)]}}

    def headers(self, session_id: str) -> dict[str, str]:
        return {MCP_SESSION_HEADER: session_id, MCP_PROTOCOL_HEADER: PROTOCOL}

    def test_each_client_field_is_encoded_once_however_often_the_table_is_written(self) -> None:
        info, capabilities = json.dumps(self.INFO), json.dumps(self.CAPABILITIES)
        encode = json.dumps
        encoded: list[str] = []

        def counted(*args, **kwargs) -> str:
            text = encode(*args, **kwargs)
            encoded.append(text)
            return text

        with (
            patch.object(mcp_transport.json, "dumps", side_effect=counted),
            patch.object(mcp_transport, "atomic_write_text", wraps=mcp_transport.atomic_write_text) as writes,
        ):
            made = self.assert_an_ordinary_answer(
                self.initialize_as_written(client_info=info, capabilities=capabilities)
            )
            ready = self.client.post("/mcp", headers=self.headers(made), json=NOTIFIED)
            another = self.assert_an_ordinary_answer(self.initialize())
            deleted = self.delete(another)

        self.assertEqual((ready.status_code, deleted.status_code), (202, 204))
        # Four writes of the whole table, each with that session in it, and each of its two fields encoded once: to be
        # measured, when the session was made.
        self.assertEqual(writes.call_count, 4)
        markers = ("INFO-OF-THE-CLIENT", "CAPABILITIES-OF-THE-CLIENT")
        self.assertEqual([sum(marker in text for text in encoded) for marker in markers], [1, 1])
        (record,) = self.stored()
        self.assertEqual((record["client_info"], record["client_capabilities"]), (self.INFO, self.CAPABILITIES))

    def test_the_table_is_what_encoding_it_whole_gives_and_reads_back_the_same(self) -> None:
        # Its shape is unchanged, so each controller, older ones included, reads it as before.
        plain = self.assert_an_ordinary_answer(self.initialize(self.INFO, self.CAPABILITIES))
        self.assert_an_ordinary_answer(self.initialize(CLIENT_INFO, CAPABILITIES))  # stored with U+FFFD
        self.assert_an_ordinary_answer(
            self.initialize_as_written(client_info=_objects(DEPTH_BOUND), capabilities=_of_bytes(BYTES_BOUND))
        )
        self.assertEqual(self.client.post("/mcp", headers=self.headers(plain), json=NOTIFIED).status_code, 202)
        self.assertEqual(self.client.post("/mcp", headers=self.headers(plain), json=SUBSCRIBED).status_code, 200)

        sessions = list(self.transport._sessions.values())
        whole = json.dumps([asdict(session) for session in sessions], ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(self.store.read_text(encoding="utf-8"), whole)
        self.assertEqual(list(self.transport_over_the_store()._sessions.values()), sessions)

    def test_a_sessions_other_fields_are_written_as_they_are_at_each_write(self) -> None:
        made = self.assert_an_ordinary_answer(self.initialize(self.INFO, self.CAPABILITIES))
        self.assertEqual(
            [(item["initialized"], item["resource_subscriptions"]) for item in self.stored()], [(False, [])]
        )

        self.assertEqual(self.client.post("/mcp", headers=self.headers(made), json=NOTIFIED).status_code, 202)
        self.assertEqual(self.client.post("/mcp", headers=self.headers(made), json=SUBSCRIBED).status_code, 200)
        session = self.transport._sessions[made]
        # Each saved with the next write of the table, here another client's initialize. The owner is set by hand: a
        # request of this test's client could no longer reach the session.
        session.last_used_at = "2026-11-11T00:00:00Z"
        session.owner = "operator-a"
        self.assert_an_ordinary_answer(self.initialize())

        record = self.stored()[0]
        self.assertEqual(record, json.loads(json.dumps(asdict(session))))
        written = (record["initialized"], record["resource_subscriptions"], record["last_used_at"], record["owner"])
        self.assertEqual(written, (True, ["browser://sessions"], "2026-11-11T00:00:00Z", "operator-a"))

    def test_a_repeated_notifications_initialized_writes_nothing(self) -> None:
        # Each one wrote the whole table, so a client could send it again and again for an initialized session.
        made = self.assert_an_ordinary_answer(self.initialize())

        with patch.object(mcp_transport, "atomic_write_text", wraps=mcp_transport.atomic_write_text) as writes:
            answers = [self.client.post("/mcp", headers=self.headers(made), json=NOTIFIED) for _ in range(3)]

        self.assertEqual([answer.status_code for answer in answers], [202, 202, 202])
        self.assertEqual(writes.call_count, 1)
        self.assertEqual([record["initialized"] for record in self.stored()], [True])

    def test_a_notifications_initialized_whose_write_fails_is_written_by_its_retry(self) -> None:
        # The session was marked initialized before the write: one that failed left it so in memory, the retry wrote
        # nothing and answered 202, and after a restart the client was told to initialize (-32005).
        made = self.assert_an_ordinary_answer(self.initialize())

        with patch.object(mcp_transport, "atomic_write_text", side_effect=OSError(28, "No space left on device")):
            failed = self.client.post("/mcp", headers=self.headers(made), json=NOTIFIED)
        retried = self.client.post("/mcp", headers=self.headers(made), json=NOTIFIED)

        self.assertEqual((failed.status_code, retried.status_code), (500, 202))
        self.assertEqual([record["initialized"] for record in self.stored()], [True])
        self.transport = self.transport_over_the_store()
        listed = self.client.post(
            "/mcp", headers=self.headers(made), json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        self.assertEqual(listed.json(), {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})


if __name__ == "__main__":
    unittest.main()
