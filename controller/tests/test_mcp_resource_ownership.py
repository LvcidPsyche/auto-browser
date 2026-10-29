"""MCP resources name only the sessions the caller may use.

Reading another operator's session resource has been refused since the session
ownership release, but resources/list still handed out every operator's session
ids (and so what to subscribe to), and the update stream would then notify a
subscriber about activity in a session it cannot read. Unowned sessions (shared
token, or no token) are everyone's, as everywhere else.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

from app.mcp_transport import MCP_PROTOCOL_HEADER, MCP_SESSION_HEADER, McpHttpTransport, McpSession
from app.models import OperatorIdentity
from app.session_ownership import acting_as

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")
ALICE_BY_HEADER = OperatorIdentity(id="alice", source="header")
PROTOCOL = "2025-11-25"


def _uris(resources: list[dict]) -> set[str]:
    return {resource["uri"] for resource in resources}


class McpResourceOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        # Closed sessions keep their record, and so their owner.
        records = {"gone-1": "alice", "gone-shared": None}

        async def session_owner_lookup(session_id: str) -> tuple[bool, str | None]:
            if session_id == "unreadable-1":
                raise OSError("session store unavailable")
            return session_id in records, records.get(session_id)

        self.manager = SimpleNamespace(
            sessions={
                "alice-1": SimpleNamespace(owner="alice"),
                "bob-1": SimpleNamespace(owner="bob"),
                "shared-1": SimpleNamespace(owner=None),
            },
            session_owner_lookup=session_owner_lookup,
        )
        self.transport = McpHttpTransport(
            tool_gateway=SimpleNamespace(list_tools=lambda: []),
            server_name="auto-browser",
            server_version="0.2.0",
            session_store_path=f"{self.tempdir.name}/mcp-sessions.json",
            manager=self.manager,
        )

    @staticmethod
    def session_ids(resources: list[dict]) -> set[str]:
        return {uri.split("/")[2] for uri in _uris(resources) if uri.count("/") == 3}

    async def list_as(self, operator: OperatorIdentity | None) -> list[dict]:
        with acting_as(operator):
            return await self.transport._list_resources()

    async def test_list_names_only_sessions_the_caller_may_use(self) -> None:
        self.assertEqual(self.session_ids(await self.list_as(ALICE)), {"alice-1", "shared-1"})
        self.assertEqual(self.session_ids(await self.list_as(BOB)), {"bob-1", "shared-1"})
        # Asserted by a header, or no identity at all: only what is unowned.
        self.assertEqual(self.session_ids(await self.list_as(ALICE_BY_HEADER)), {"shared-1"})
        self.assertEqual(self.session_ids(await self.list_as(None)), {"shared-1"})

    async def test_list_keeps_the_aggregate_resources_and_every_kind_per_session(self) -> None:
        uris = _uris(await self.list_as(ALICE))

        self.assertIn("browser://sessions", uris)
        for kind in ("screenshot", "dom", "console", "network"):
            self.assertIn(f"browser://alice-1/{kind}", uris)
            self.assertNotIn(f"browser://bob-1/{kind}", uris)

    async def test_single_operator_deployment_lists_everything(self) -> None:
        self.manager.sessions = {"one": SimpleNamespace(owner=None), "two": SimpleNamespace(owner=None)}

        self.assertEqual(self.session_ids(await self.list_as(None)), {"one", "two"})
        self.assertEqual(self.session_ids(await self.list_as(BOB)), {"one", "two"})

    async def test_cannot_subscribe_to_another_operators_session_resource(self) -> None:
        mcp_session = McpSession(id="m1", protocol_version=PROTOCOL, client_info={}, client_capabilities={})

        async def subscribe(operator: OperatorIdentity, uri: str) -> dict:
            with acting_as(operator):
                response = await self.transport._handle_resource_subscribe(1, mcp_session, {"uri": uri})
            return json.loads(response.body)

        refused = await subscribe(BOB, "browser://alice-1/dom")
        self.assertEqual(refused["error"]["code"], -32002)
        self.assertEqual(mcp_session.resource_subscriptions, [])

        accepted = await subscribe(BOB, "browser://bob-1/dom")
        self.assertEqual(accepted["result"], {})
        self.assertEqual(mcp_session.resource_subscriptions, ["browser://bob-1/dom"])

    async def stream_as(
        self, operator: OperatorIdentity | None, events: list[dict], subscribed: list[str]
    ) -> list[str]:
        """What a subscriber receives from the update stream.

        The stream is opened inside the request, where the operator is known, and
        read afterwards with no operator set: the HTTP middleware resets it once
        the handler returns, and the body is streamed after that.
        """
        queue: asyncio.Queue[str] = asyncio.Queue()
        for event in events:
            queue.put_nowait(json.dumps(event))
        mcp_session = McpSession(
            id="m1",
            protocol_version=PROTOCOL,
            client_info={},
            client_capabilities={},
            initialized=True,
            resource_subscriptions=sorted(subscribed),
        )
        self.transport._sessions["m1"] = mcp_session
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/mcp",
                "query_string": b"",
                "headers": [
                    (MCP_SESSION_HEADER.lower().encode(), b"m1"),
                    (MCP_PROTOCOL_HEADER.lower().encode(), PROTOCOL.encode()),
                ],
            }
        )

        async def disconnected() -> bool:
            return queue.empty()

        request.is_disconnected = disconnected  # type: ignore[method-assign]
        with patch("app.mcp_transport._events.subscribe_all", return_value=queue):
            with acting_as(operator):
                response = await self.transport.handle_get_request(request)
            return [chunk async for chunk in response.body_iterator if "notifications/resources/updated" in chunk]

    @staticmethod
    def notified(chunks: list[str]) -> list[str]:
        return [json.loads(chunk.split("data: ", 1)[1])["params"]["uri"] for chunk in chunks]

    async def test_stream_does_not_notify_about_a_session_the_subscriber_may_not_use(self) -> None:
        subscribed = ["browser://sessions", "browser://alice-1/dom", "browser://bob-1/dom"]
        events = [
            {"event": "action", "session_id": "alice-1"},
            {"event": "observe", "session_id": "bob-1"},
        ]

        bob = self.notified(await self.stream_as(BOB, events, subscribed))

        self.assertEqual(sorted(bob), ["browser://bob-1/dom", "browser://sessions"])

    async def test_stream_still_notifies_the_owner_and_everyone_for_unowned_sessions(self) -> None:
        subscribed = ["browser://sessions", "browser://alice-1/dom", "browser://shared-1/dom"]
        events = [
            {"event": "observe", "session_id": "alice-1"},
            {"event": "observe", "session_id": "shared-1"},
        ]

        alice = self.notified(await self.stream_as(ALICE, events, subscribed))
        # The list resource is reported once per event.
        self.assertEqual(
            sorted(alice),
            ["browser://alice-1/dom", "browser://sessions", "browser://sessions", "browser://shared-1/dom"],
        )
        anyone = self.notified(await self.stream_as(None, events[1:], subscribed))
        self.assertEqual(sorted(anyone), ["browser://sessions", "browser://shared-1/dom"])

    async def test_stream_still_reports_a_session_that_has_just_closed(self) -> None:
        # The close event is emitted after the session leaves manager.sessions; its
        # owner is then read from its record.
        subscribed = ["browser://sessions", "browser://alice-1/dom"]
        closed = [{"event": "session", "session_id": "gone-1", "status": "closed"}]

        self.assertEqual(self.notified(await self.stream_as(ALICE, closed, subscribed)), ["browser://sessions"])

    async def test_stream_does_not_report_another_operators_session_closing(self) -> None:
        """The generic browser://sessions update told every subscriber when another operator's session closed."""
        closed = [{"event": "session", "session_id": "gone-1", "status": "closed"}]
        unowned = [{"event": "session", "session_id": "gone-shared", "status": "closed"}]
        unknown = [{"event": "session", "session_id": "never-1", "status": "closed"}]

        self.assertEqual(self.notified(await self.stream_as(BOB, closed, ["browser://sessions"])), [])
        self.assertEqual(
            self.notified(await self.stream_as(BOB, unowned, ["browser://sessions"])), ["browser://sessions"]
        )
        # The owner could not be read: the update is withheld, not sent.
        unreadable = [{"event": "session", "session_id": "unreadable-1", "status": "closed"}]
        self.assertEqual(self.notified(await self.stream_as(BOB, unreadable, ["browser://sessions"])), [])
        # No live session and no record names no operator, so it is not held back.
        self.assertEqual(
            self.notified(await self.stream_as(BOB, unknown, ["browser://sessions"])), ["browser://sessions"]
        )


def mcp_request(http_method: str, mcp_session_id: str | None = None, body: dict | None = None) -> Request:
    headers = [(b"content-type", b"application/json"), (MCP_PROTOCOL_HEADER.lower().encode(), PROTOCOL.encode())]
    if mcp_session_id is not None:
        headers.append((MCP_SESSION_HEADER.lower().encode(), mcp_session_id.encode()))
    payload = json.dumps(body or {}).encode()

    async def receive() -> dict:
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {"type": "http", "method": http_method, "path": "/mcp", "query_string": b"", "headers": headers}
    return Request(scope, receive)


class McpTransportSessionOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """An MCP session (MCP-Session-Id) belongs to the operator who initialized it.

    Another operator holding the id could ping it, change its subscriptions and DELETE it. Tool calls
    always ran as the bearer's own operator, so no browser session was reachable this way.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = f"{self.tempdir.name}/mcp-sessions.json"
        self.manager = SimpleNamespace(sessions={})
        self.transport = self.new_transport()

    def new_transport(self) -> McpHttpTransport:
        return McpHttpTransport(
            tool_gateway=SimpleNamespace(list_tools=lambda: []),
            server_name="auto-browser",
            server_version="0.2.0",
            session_store_path=self.store,
            manager=self.manager,
        )

    async def initialize_as(self, operator: OperatorIdentity | None) -> str:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL}}
        with acting_as(operator):
            response = await self.transport.handle_post_request(mcp_request("POST", body=init))
            mcp_session_id = response.headers[MCP_SESSION_HEADER]
            ready = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            await self.transport.handle_post_request(mcp_request("POST", mcp_session_id, ready))
        return mcp_session_id

    async def call_as(self, operator: OperatorIdentity | None, mcp_session_id: str, method: str, params=None):
        body = {"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}}
        with acting_as(operator):
            return await self.transport.handle_post_request(mcp_request("POST", mcp_session_id, body))

    def assert_unknown(self, response, mcp_session_id: str) -> None:
        self.assertEqual(response.status_code, 404)
        error = json.loads(response.body)["error"]
        self.assertEqual((error["code"], error["message"]), (-32001, f"Unknown MCP session: {mcp_session_id}"))

    async def test_another_operator_cannot_use_or_delete_an_mcp_session(self) -> None:
        mcp_session_id = await self.initialize_as(ALICE)

        for intruder in (BOB, ALICE_BY_HEADER, None):
            with self.subTest(intruder=intruder):
                self.assert_unknown(await self.call_as(intruder, mcp_session_id, "ping"), mcp_session_id)
                subscribe = await self.call_as(
                    intruder, mcp_session_id, "resources/subscribe", {"uri": "browser://sessions"}
                )
                self.assert_unknown(subscribe, mcp_session_id)
                with acting_as(intruder):
                    self.assert_unknown(
                        await self.transport.handle_get_request(mcp_request("GET", mcp_session_id)), mcp_session_id
                    )
                    deleted = await self.transport.handle_delete_request(mcp_request("DELETE", mcp_session_id))
                self.assert_unknown(deleted, mcp_session_id)

        self.assertEqual(self.transport._sessions[mcp_session_id].resource_subscriptions, [])
        self.assertEqual((await self.call_as(ALICE, mcp_session_id, "ping")).status_code, 200)
        with acting_as(ALICE):
            self.assertEqual(
                (await self.transport.handle_delete_request(mcp_request("DELETE", mcp_session_id))).status_code, 204
            )

    async def test_an_unowned_mcp_session_stays_open_to_everyone(self) -> None:
        for creator in (None, ALICE_BY_HEADER):
            with self.subTest(creator=creator):
                mcp_session_id = await self.initialize_as(creator)
                self.assertEqual((await self.call_as(BOB, mcp_session_id, "ping")).status_code, 200)

    async def test_an_mcp_sessions_owner_survives_a_restart(self) -> None:
        mcp_session_id = await self.initialize_as(ALICE)

        self.transport = self.new_transport()

        self.assert_unknown(await self.call_as(BOB, mcp_session_id, "ping"), mcp_session_id)
        self.assertEqual((await self.call_as(ALICE, mcp_session_id, "ping")).status_code, 200)


if __name__ == "__main__":
    unittest.main()
