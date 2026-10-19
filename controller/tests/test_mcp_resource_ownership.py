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
        self.manager = SimpleNamespace(
            sessions={
                "alice-1": SimpleNamespace(owner="alice"),
                "bob-1": SimpleNamespace(owner="bob"),
                "shared-1": SimpleNamespace(owner=None),
            }
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
        # The close event is emitted after the session leaves manager.sessions, so
        # its owner is no longer known; the subscriber's own subscriptions are
        # already limited to what it may use, and the generic list URI names no one.
        subscribed = ["browser://sessions", "browser://alice-1/dom"]

        alice = self.notified(
            await self.stream_as(ALICE, [{"event": "session", "session_id": "gone-1", "status": "closed"}], subscribed)
        )

        self.assertEqual(alice, ["browser://sessions"])


if __name__ == "__main__":
    unittest.main()
