"""An open event stream does not keep a stop from reaching the cleanup.

On SIGTERM uvicorn waits for the responses in flight before it runs the lifespan's shutdown, with no limit unless
--timeout-graceful-shutdown gives one. A listening client's GET /mcp stream, and GET /sessions/{id}/events, never end
by themselves, so maintenance, the cron service, the job queue and the manager were never shut down: Docker killed
the process at stop_grace_period, and sessions, containers and tunnels were left as they were.

Two halves. The start commands each give the limit (a repo-layout check: the controller image ships only app/ and
tests/, so it skips there). And at the protocol level, uvicorn's own Server.shutdown() and HTTP protocol class run
over a transport that opens no socket, with the real app behind them and both streams open: the lifespan's shutdown
is reached, nothing of the streams is left, and the stop does not read as a crash in the log.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import uvicorn

from app import events
from app.middleware.http import UVICORN_SHUTDOWN_CANCEL

REPO = Path(__file__).resolve().parents[2]
FLAG = "--timeout-graceful-shutdown"
STARTS_THE_APP = "app.main:app"
# Every tracked place that could start the controller's uvicorn: an image's command, a script, a compose file, the
# Makefile, and the run lines in the docs.
START_FILES = (
    "Dockerfile",
    "controller/Dockerfile",
    "controller/entrypoint.sh",
    "scripts/*.sh",
    "docker-compose*.yml",
    "Makefile",
    "README.md",
    "docs/*.md",
    ".devcontainer/*",
)


def _start_lines() -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    for pattern in START_FILES:
        for path in sorted(REPO.glob(pattern)):
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if STARTS_THE_APP in line and not line.lstrip().startswith("#"):
                    lines.append((path.relative_to(REPO).as_posix(), line.strip()))
    return lines


def _limit(line: str) -> int | None:
    # `--timeout-graceful-shutdown 5` in a shell line, `"--timeout-graceful-shutdown", "5"` in an exec-form CMD.
    match = re.search(rf'{FLAG}"?[,\s]+"?(\d+)', line)
    return int(match.group(1)) if match else None


@unittest.skipUnless((REPO / "controller" / "Dockerfile").is_file(), "the repo's start commands are not in this tree")
class EveryStartCommandGivesALimitTests(unittest.TestCase):
    def test_the_image_and_the_glama_entrypoint_are_seen(self) -> None:
        # Guards the scan against a layout change that would make the checks below vacuous.
        files = {name for name, _ in _start_lines()}
        self.assertLessEqual({"controller/Dockerfile", "scripts/glama_entrypoint.sh"}, files)

    def test_each_command_that_starts_the_app_gives_uvicorn_a_graceful_shutdown_limit(self) -> None:
        for name, line in _start_lines():
            with self.subTest(file=name):
                self.assertIsNotNone(_limit(line), f"{name} starts uvicorn with no {FLAG}: {line}")

    def test_the_limit_is_one_number_well_under_the_stop_grace_period(self) -> None:
        limits = {_limit(line) for _, line in _start_lines()} - {None}
        self.assertEqual(len(limits), 1, f"the start commands disagree: {limits}")
        (limit,) = limits
        compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
        grace = re.search(r"(?m)^    stop_grace_period:\s*(\d+)s\s*$", compose)
        self.assertIsNotNone(grace, "docker-compose.yml gives the controller no stop_grace_period in seconds")
        # The lifespan's cleanup closes sessions one after another: it keeps at least three quarters of the grace.
        self.assertGreater(limit, 0)
        self.assertLessEqual(limit * 4, int(grace.group(1)))
        # The compose comment says the number, so the two stay true of each other.
        self.assertIn(f"{FLAG} {limit}", compose)


class _Transport:
    """What uvicorn's protocol writes to, and nothing else: no socket."""

    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False
        self.protocol: Any = None

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return {"peername": ("127.0.0.1", 50000), "sockname": ("127.0.0.1", 8000)}.get(name, default)

    def write(self, data: bytes) -> None:
        self.written += data

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            asyncio.get_running_loop().call_soon(self.protocol.connection_lost, None)

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass

    def set_protocol(self, protocol: Any) -> None:
        self.protocol = protocol


class _Lifespan:
    def __init__(self) -> None:
        self.shut_down = False

    async def shutdown(self) -> None:
        self.shut_down = True


def _uvicorn_internals_missing() -> str | None:
    """Why this uvicorn cannot be driven the way these tests drive it, or None."""
    try:
        from uvicorn.protocols.http.h11_impl import H11Protocol  # noqa: F401
    except ImportError as missing:
        return f"uvicorn has no h11 protocol class here ({missing})"
    if not hasattr(uvicorn.Config, "timeout_graceful_shutdown") and "timeout_graceful_shutdown" not in (
        inspect.signature(uvicorn.Config.__init__).parameters
    ):
        return "this uvicorn has no timeout_graceful_shutdown"
    if not inspect.iscoroutinefunction(getattr(uvicorn.Server, "shutdown", None)):
        return "this uvicorn's Server has no shutdown coroutine"
    return None


@unittest.skipIf(_uvicorn_internals_missing() is not None, _uvicorn_internals_missing() or "")
class AnOpenStreamDoesNotHoldTheShutdownTests(unittest.IsolatedAsyncioTestCase):
    LIMIT_SECONDS = 0.5

    async def asyncSetUp(self) -> None:
        import app.main as main

        self.main = main
        self.queues_before = len(events._GLOBAL_QUEUES)
        session = patch.object(main.manager, "get_session", new=AsyncMock(return_value=SimpleNamespace(id="s1")))
        session.start()
        self.addCleanup(session.stop)
        # log_config=None: uvicorn's own logging setup would stay behind for the rest of the test run.
        config = uvicorn.Config(app=main.app, lifespan="off", http="h11", log_config=None)
        config.load()
        self.config = config
        self.server = uvicorn.Server(config)
        self.server.servers = []
        self.server.lifespan = _Lifespan()

    def connect(self) -> tuple[Any, _Transport]:
        from uvicorn.protocols.http.h11_impl import H11Protocol

        protocol = H11Protocol(self.config, self.server.server_state, {})
        transport = _Transport()
        transport.protocol = protocol
        protocol.connection_made(transport)
        return protocol, transport

    @staticmethod
    def request(method: str, path: str, headers: dict[str, str], body: bytes = b"") -> bytes:
        lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1"] + [f"{key}: {value}" for key, value in headers.items()]
        if body:
            lines.append(f"Content-Length: {len(body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + body

    async def post_mcp(self, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, str]:
        protocol, transport = self.connect()
        body = json.dumps(payload).encode()
        protocol.data_received(self.request("POST", "/mcp", {"Content-Type": "application/json", **headers}, body))
        for _ in range(200):
            await asyncio.sleep(0.02)
            if protocol.cycle is not None and protocol.cycle.response_complete:
                break
        head = bytes(transport.written).split(b"\r\n\r\n", 1)[0].decode()
        transport.close()
        await asyncio.sleep(0.02)
        self.assertIn(head.split("\r\n")[0], ("HTTP/1.1 200 OK", "HTTP/1.1 202 Accepted"), head)
        return {
            key.strip().lower(): value.strip()
            for key, _, value in (line.partition(":") for line in head.split("\r\n")[1:])
        }

    async def open_streams(self) -> list[_Transport]:
        answered = await self.post_mcp(
            {}, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}
        )
        session = {
            "MCP-Session-Id": answered["mcp-session-id"],
            "MCP-Protocol-Version": answered["mcp-protocol-version"],
        }
        await self.post_mcp(session, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        streams = []
        for path, headers in (("/mcp", {"Accept": "text/event-stream", **session}), ("/sessions/s1/events", {})):
            protocol, transport = self.connect()
            protocol.data_received(self.request("GET", path, headers))
            streams.append(transport)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if all(b"\r\n\r\n" in bytes(transport.written) for transport in streams):
                break
        for transport in streams:
            self.assertTrue(bytes(transport.written).startswith(b"HTTP/1.1 200 OK"), bytes(transport.written)[:80])
        return streams

    async def test_with_the_limit_the_lifespans_shutdown_is_reached_and_nothing_of_the_streams_is_left(self) -> None:
        streams = await self.open_streams()
        self.assertEqual(len(self.server.server_state.connections), 2)
        self.assertEqual(len(self.main.mcp_transport._event_streams), 1)
        self.config.timeout_graceful_shutdown = self.LIMIT_SECONDS

        with (
            self.assertLogs("uvicorn.error", level="INFO") as server_log,
            self.assertLogs("app.middleware.http", level="INFO") as ended,
        ):
            await asyncio.wait_for(self.server.shutdown(), 10)
            # shutdown() cancels the stream tasks and does not wait for them: they unwind after it has returned.
            for _ in range(250):
                if not self.server.server_state.tasks and not self.server.server_state.connections:
                    break
                await asyncio.sleep(0.02)

        self.assertTrue(self.server.lifespan.shut_down, "the lifespan's shutdown was not reached")
        self.assertEqual(len(self.server.server_state.connections), 0)
        self.assertEqual(len(self.server.server_state.tasks), 0)
        self.assertEqual(self.main.mcp_transport._event_streams, {})
        self.assertEqual(len(events._GLOBAL_QUEUES), self.queues_before)
        # A normal stop must not read as a crash: no traceback for a stream uvicorn cancelled at its limit. uvicorn's
        # own line that it cancelled them stays, at ERROR; the docs say what it means.
        errors = [record.getMessage() for record in server_log.records if record.levelno >= logging.ERROR]
        self.assertEqual(errors, ["Cancel 2 running task(s), timeout graceful shutdown exceeded"])
        self.assertEqual([record.levelname for record in ended.records], ["INFO", "INFO"], ended.output)
        for transport in streams:
            # Each stream's body ends as a finished chunked body does, and its connection is closed.
            self.assertTrue(bytes(transport.written).endswith(b"0\r\n\r\n"), bytes(transport.written)[-40:])
            self.assertTrue(transport.closed)

    async def test_control_with_no_limit_the_shutdown_never_reaches_the_lifespan(self) -> None:
        # What every start command gave before: None.
        await self.open_streams()
        self.assertIsNone(self.config.timeout_graceful_shutdown)

        shutdown = asyncio.ensure_future(self.server.shutdown())
        await asyncio.sleep(self.LIMIT_SECONDS * 3)

        self.assertFalse(shutdown.done(), "an open stream no longer holds an unlimited shutdown: this file is stale")
        self.assertFalse(self.server.lifespan.shut_down)
        # Ended as uvicorn ends them at a limit, so the test leaves nothing running.
        for task in list(self.server.server_state.tasks):
            task.cancel(msg=UVICORN_SHUTDOWN_CANCEL)
        await asyncio.wait_for(shutdown, 10)
        self.assertTrue(self.server.lifespan.shut_down)

    async def test_the_cancellation_uvicorn_sends_is_the_one_the_app_ends_quietly(self) -> None:
        # The app tells uvicorn's shutdown cancel from any other by its message, which is uvicorn's own text.
        self.assertIn(UVICORN_SHUTDOWN_CANCEL, inspect.getsource(uvicorn.Server.shutdown))


class OnlyUvicornsShutdownCancelIsEndedQuietlyTests(unittest.IsolatedAsyncioTestCase):
    """The layer itself, with no server: what it ends, and what it leaves to propagate."""

    async def run_stream(self, *, content_type: bytes, cancel_message: str | None, started: bool = True) -> list[dict]:
        from app.middleware.http import EventStreamShutdownMiddleware

        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        async def downstream(scope: dict, receive: Any, send: Any) -> None:
            if started:
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", content_type)]})
                await send({"type": "http.response.body", "body": b": connected\n\n", "more_body": True})
            raise asyncio.CancelledError(*([cancel_message] if cancel_message is not None else []))

        layer = EventStreamShutdownMiddleware(downstream)
        await layer({"type": "http", "path": "/mcp", "method": "GET"}, AsyncMock(), send)
        return sent

    async def test_a_started_event_stream_cancelled_at_the_shutdown_limit_ends_with_a_last_empty_chunk(self) -> None:
        with self.assertLogs("app.middleware.http", level="INFO"):
            sent = await self.run_stream(
                content_type=b"text/event-stream; charset=utf-8", cancel_message=UVICORN_SHUTDOWN_CANCEL
            )

        self.assertEqual(sent[-1], {"type": "http.response.body", "body": b"", "more_body": False})

    async def test_any_other_cancellation_of_a_stream_propagates(self) -> None:
        for message in (None, "Cancelled by cancel scope 1f3", "deadline"):
            with self.subTest(message=message), self.assertRaises(asyncio.CancelledError):
                await self.run_stream(content_type=b"text/event-stream", cancel_message=message)

    async def test_a_response_that_is_no_event_stream_propagates_the_shutdown_cancel(self) -> None:
        with self.assertRaises(asyncio.CancelledError):
            await self.run_stream(content_type=b"application/json", cancel_message=UVICORN_SHUTDOWN_CANCEL)

    async def test_a_stream_cancelled_before_its_response_started_propagates(self) -> None:
        with self.assertRaises(asyncio.CancelledError):
            await self.run_stream(
                content_type=b"text/event-stream", cancel_message=UVICORN_SHUTDOWN_CANCEL, started=False
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
