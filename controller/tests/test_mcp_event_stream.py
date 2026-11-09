"""One live GET /mcp stream per MCP session, and a stream ends the moment its session does.

Every GET opened another stream and another subscribe_all queue for the same session, each told about every
event, and a DELETE or an eviction left them all running for a session that no longer existed. A new GET now takes
over synchronously, and a stopped stream ends at once, not at its next keepalive wake, on every way out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from starlette.requests import Request
from starlette.responses import StreamingResponse

from app import events
from app import mcp_transport as transport_module
from app.mcp_transport import (
    MAX_MCP_SESSIONS,
    MAX_MCP_SESSIONS_PER_OWNER,
    MCP_PROTOCOL_HEADER,
    MCP_SESSION_HEADER,
    McpHttpTransport,
    McpSession,
)
from app.session_ownership import acting_as
from tests.test_mcp_resource_ownership import ALICE, BOB, PROTOCOL, mcp_request

# Well under the 15 s keepalive wake, the bound a stopped stream used to have.
PROMPTLY = 2.0
OLD = "2000-01-01T00:00:00Z"


def get_request(session_id: str, *, disconnected: asyncio.Event | None = None) -> Any:
    request = mcp_request("GET", session_id)

    async def is_disconnected() -> bool:
        return disconnected is not None and disconnected.is_set()

    request.is_disconnected = is_disconnected
    return request


class OpenStream:
    """A GET /mcp whose body is read by a task, as the server reads it, parked in the stream's wait."""

    def __init__(self, response: StreamingResponse) -> None:
        self.response = response
        self.chunks: list[str] = []
        self.first_chunk = asyncio.Event()
        self.task = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        async for chunk in self.response.body_iterator:
            self.chunks.append(chunk)
            self.first_chunk.set()

    async def parked(self) -> None:
        await asyncio.wait_for(self.first_chunk.wait(), PROMPTLY)
        await asyncio.sleep(0)

    async def ended(self) -> None:
        """Fails if the stream is still running after PROMPTLY seconds."""
        await asyncio.wait_for(asyncio.shield(self.task), PROMPTLY)

    async def finish(self) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task

    def notifications(self) -> list[str]:
        return [chunk for chunk in self.chunks if "notifications/resources/updated" in chunk]


class McpEventStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = f"{self.tempdir.name}/mcp-sessions.json"
        self.manager = SimpleNamespace(sessions={}, session_owner_lookup=AsyncMock(return_value=(False, None)))
        self.transport = McpHttpTransport(
            tool_gateway=SimpleNamespace(list_tools=lambda: []),
            server_name="auto-browser",
            server_version="0.2.0",
            session_store_path=self.store,
            manager=self.manager,
        )
        self.session = self.new_session("m1")

    def new_session(self, session_id: str, *, owner: str | None = None, last_used_at: str = "") -> McpSession:
        session = McpSession(
            id=session_id,
            protocol_version=PROTOCOL,
            client_info={},
            client_capabilities={},
            initialized=True,
            owner=owner,
            last_used_at=last_used_at,
        )
        self.transport._sessions[session_id] = session
        return session

    def queues(self) -> int:
        return len(events._GLOBAL_QUEUES)

    @staticmethod
    def pending_tasks() -> set[asyncio.Task]:
        return {task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()}

    async def open_stream(self, session_id: str = "m1", disconnected: asyncio.Event | None = None) -> OpenStream:
        response = await self.transport.handle_get_request(get_request(session_id, disconnected=disconnected))
        self.assertIsInstance(response, StreamingResponse)
        stream = OpenStream(response)
        self.addAsyncCleanup(stream.finish)
        await stream.parked()
        return stream

    async def delete(self, session_id: str = "m1") -> None:
        response = await self.transport.handle_delete_request(mcp_request("DELETE", session_id))
        self.assertEqual(response.status_code, 204)

    # ── Takeover ────────────────────────────────────────────────────────────

    async def test_a_second_get_ends_the_first_stream_and_keeps_the_second(self) -> None:
        queues = self.queues()
        first = await self.open_stream()
        second = await self.open_stream()
        self.session.resource_subscriptions = ["browser://sessions"]

        await first.ended()

        self.assertEqual(self.queues(), queues + 1, "the first stream's queue is released, the second's is not")
        self.assertFalse(second.task.done())
        events.emit_session("browser-1", "closed")
        await asyncio.wait_for(self.notified(second), PROMPTLY)

    @staticmethod
    async def until(condition) -> None:
        """Yields to the stream until condition holds, or until the test's own assert will report that it did not."""
        deadline = asyncio.get_running_loop().time() + PROMPTLY
        while not condition() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)

    async def notified(self, stream: OpenStream) -> None:
        await self.until(stream.notifications)
        self.assertTrue(stream.notifications())

    async def test_the_takeover_happens_when_the_get_is_handled_not_when_its_body_starts(self) -> None:
        """Registered in the generator, two GETs raced: neither body has started here when the second arrives."""
        first = await self.transport.handle_get_request(get_request("m1"))
        second = await self.transport.handle_get_request(get_request("m1"))

        chunks = await asyncio.wait_for(self.collect(first.body_iterator), PROMPTLY)

        self.assertEqual(chunks, [": connected\n\n"], "the first stream is over before its body starts")
        await second.body_iterator.aclose()

    @staticmethod
    async def collect(body) -> list[str]:
        return [chunk async for chunk in body]

    async def test_the_old_streams_cleanup_leaves_the_successors_entry(self) -> None:
        first = await self.open_stream()
        await self.open_stream()
        successor = self.transport._event_streams["m1"]

        await first.ended()

        self.assertIs(self.transport._event_streams["m1"], successor)

    async def test_a_get_that_is_refused_leaves_the_live_stream_alone(self) -> None:
        stream = await self.open_stream()
        self.session.owner = "alice"
        mismatched = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/mcp",
                "query_string": b"",
                "headers": [
                    (MCP_SESSION_HEADER.lower().encode(), b"m1"),
                    (MCP_PROTOCOL_HEADER.lower().encode(), b"2025-06-18"),
                ],
            }
        )

        with acting_as(ALICE):
            self.assertEqual((await self.transport.handle_get_request(mismatched)).status_code, 400)
        with acting_as(BOB):
            self.assertEqual((await self.transport.handle_get_request(mcp_request("GET", "m1"))).status_code, 404)
        await asyncio.sleep(0)

        self.assertFalse(stream.task.done(), "a refused GET stopped the live stream")
        self.assertIn("m1", self.transport._event_streams)

    # ── DELETE and eviction ─────────────────────────────────────────────────

    async def test_delete_ends_the_stream_and_releases_its_queue(self) -> None:
        queues = self.queues()
        before = self.pending_tasks()
        stream = await self.open_stream()

        await self.delete()

        await stream.ended()
        self.assertEqual(self.queues(), queues)
        self.assertNotIn("m1", self.transport._event_streams)
        self.assertEqual(self.pending_tasks(), before)

    async def test_eviction_past_an_owners_cap_ends_that_sessions_stream(self) -> None:
        self.transport._sessions.clear()
        for index in range(MAX_MCP_SESSIONS_PER_OWNER):
            self.new_session(f"newer-{index}", owner="alice", last_used_at=f"2026-01-01T00:00:{index % 60:02d}Z")
        evicted = self.new_session("evicted", owner="alice")
        with acting_as(ALICE):
            stream = await self.open_stream("evicted")
        evicted.last_used_at = OLD
        queues = self.queues()

        self.transport._evict_stale_sessions()

        self.assertNotIn("evicted", self.transport._sessions)
        await stream.ended()
        self.assertEqual(self.queues(), queues - 1)
        self.assertNotIn("evicted", self.transport._event_streams)

    async def test_eviction_by_the_table_cap_ends_that_sessions_stream(self) -> None:
        self.transport._sessions.clear()
        for index in range(MAX_MCP_SESSIONS):
            self.new_session(f"newer-{index}", last_used_at=f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z")
        evicted = self.new_session("evicted")
        stream = await self.open_stream("evicted")
        evicted.last_used_at = OLD
        queues = self.queues()

        self.transport._evict_stale_sessions()

        self.assertNotIn("evicted", self.transport._sessions)
        await stream.ended()
        self.assertEqual(self.queues(), queues - 1)

    async def test_an_open_stream_counts_as_use_and_is_not_the_one_evicted(self) -> None:
        """A listening client whose stream ended on eviction lost it for no reason: it is the busiest in the table."""
        self.transport._sessions.clear()
        for index in range(MAX_MCP_SESSIONS):
            self.new_session(f"other-{index}", last_used_at=f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z")
        listening = self.new_session("listening")
        stream = await self.open_stream("listening")
        listening.last_used_at = OLD

        # A turn of the stream's loop that reaches neither the keepalive nor a notification.
        events.emit_session("no-such-browser-session", "closed")
        await self.until(lambda: listening.last_used_at > OLD)
        self.transport._evict_stale_sessions()

        self.assertGreater(listening.last_used_at, OLD)
        self.assertIn("listening", self.transport._sessions)
        self.assertNotIn("other-0", self.transport._sessions)
        self.assertFalse(stream.task.done())

    async def test_a_turn_of_the_stream_does_not_write_the_store(self) -> None:
        """It is saved with the next store write, as last_used_at's comment says."""
        await self.open_stream()
        self.session.last_used_at = OLD

        with patch.object(transport_module, "atomic_write_text") as write:
            events.emit_session("no-such-browser-session", "closed")
            await self.until(lambda: self.session.last_used_at > OLD)

        write.assert_not_called()
        self.assertGreater(self.session.last_used_at, OLD)
        self.transport._persist_sessions()
        with open(self.store, encoding="utf-8") as stored:
            self.assertEqual(json.load(stored)[0]["last_used_at"], self.session.last_used_at)

    # ── Disconnects, close and cancel ───────────────────────────────────────

    async def test_a_client_that_disconnects_ends_the_stream_at_its_next_wake(self) -> None:
        queues = self.queues()
        before = self.pending_tasks()
        disconnected = asyncio.Event()
        stream = await self.open_stream(disconnected=disconnected)

        disconnected.set()
        events.emit_session("no-such-browser-session", "closed")

        await stream.ended()
        self.assertEqual(self.queues(), queues)
        self.assertNotIn("m1", self.transport._event_streams)
        self.assertEqual(self.pending_tasks(), before)

    async def test_closing_the_generator_releases_the_queue_and_the_entry(self) -> None:
        queues = self.queues()
        before = self.pending_tasks()
        response = await self.transport.handle_get_request(get_request("m1"))
        body = response.body_iterator

        self.assertEqual(await asyncio.wait_for(body.__anext__(), PROMPTLY), ": connected\n\n")
        await asyncio.wait_for(body.aclose(), PROMPTLY)

        self.assertEqual(self.queues(), queues)
        self.assertNotIn("m1", self.transport._event_streams)
        self.assertEqual(self.pending_tasks(), before)

    async def test_cancelling_a_stream_parked_in_its_wait_leaves_no_task_and_no_queue(self) -> None:
        queues = self.queues()
        before = self.pending_tasks()
        stream = await self.open_stream()

        stream.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(stream.task, PROMPTLY)
        await asyncio.sleep(0)

        self.assertEqual(self.queues(), queues)
        self.assertNotIn("m1", self.transport._event_streams)
        self.assertEqual(self.pending_tasks(), before)

    async def test_an_event_landing_with_the_stop_does_not_crash_the_stream(self) -> None:
        self.session.resource_subscriptions = ["browser://sessions"]
        for label, order in (
            ("the event first", lambda: (events.emit_session("b1", "closed"), self.transport._stop_event_stream("m1"))),
            ("the stop first", lambda: (self.transport._stop_event_stream("m1"), events.emit_session("b1", "closed"))),
        ):
            with self.subTest(label):
                stream = await self.open_stream()
                queues = self.queues()

                order()

                await stream.ended()
                self.assertTrue(stream.task.done())
                self.assertIsNone(stream.task.exception())
                self.assertEqual(self.queues(), queues - 1)

    async def test_the_stream_still_says_keepalive_while_it_waits(self) -> None:
        with patch.object(transport_module, "_STREAM_KEEPALIVE_SECONDS", 0.01):
            stream = await self.open_stream()

            async def kept_alive() -> None:
                while ": keepalive\n\n" not in stream.chunks:
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(kept_alive(), PROMPTLY)

        self.assertFalse(stream.task.done())


if __name__ == "__main__":
    unittest.main()
