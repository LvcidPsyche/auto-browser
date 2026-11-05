"""Two concurrent session-less calls create one session, not two."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.audit import set_current_operator
from app.models import McpToolCallRequest
from app.session_ownership import verified_operator
from app.tool_gateway.gateway import McpToolGateway

# Long enough for a loaded machine; what the tests wait on is an event, not a duration.
WAIT_SECONDS = 30
# A call that waits on nothing answers well within this; one queued behind the held create never does.
PROMPT_SECONDS = 5


class ImplicitSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_session_less_calls_create_exactly_one_session(self) -> None:
        live: list[dict] = []

        async def list_sessions():
            await asyncio.sleep(0)
            return [dict(item) for item in live]

        async def create_session(**kwargs):
            await asyncio.sleep(0.05)
            created = {"id": f"session-{len(live) + 1}", "live": True}
            live.append(created)
            return {"id": created["id"]}

        manager = SimpleNamespace(
            list_sessions=list_sessions,
            create_session=AsyncMock(side_effect=create_session),
            ensure_session_accessible=AsyncMock(),
            observe=AsyncMock(return_value={"session": {"id": "session-1"}, "url": "https://example.com"}),
        )
        gateway = McpToolGateway(
            manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
        )

        responses = await asyncio.gather(
            *(gateway.call_tool(McpToolCallRequest(name="browser.observe", arguments={})) for _ in range(2))
        )

        self.assertEqual(manager.create_session.await_count, 1)
        self.assertEqual([item["id"] for item in live], ["session-1"])
        self.assertEqual([response.isError for response in responses], [False, False])
        self.assertEqual({call.args[0] for call in manager.observe.await_args_list}, {"session-1"})


class ImplicitSessionLockScopeTests(unittest.IsolatedAsyncioTestCase):
    """Round 4: the lock was gateway-wide and held across create_session (seconds), so every session-less call,
    from every operator and from tools that never create, waited behind one slow or hung create."""

    async def asyncSetUp(self) -> None:
        self.live: list[dict] = []
        self.creating: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}

        async def list_sessions():
            owner = verified_operator()
            return [dict(item) for item in self.live if item["owner"] in (None, owner)]

        async def create_session(**kwargs):
            owner = verified_operator()
            self.creating.setdefault(str(owner), asyncio.Event()).set()
            await self.release.setdefault(str(owner), asyncio.Event()).wait()
            created = {"id": f"session-{owner}", "live": True, "owner": owner}
            self.live.append(created)
            return {"id": created["id"]}

        self.manager = SimpleNamespace(
            list_sessions=list_sessions,
            create_session=AsyncMock(side_effect=create_session),
            ensure_session_accessible=AsyncMock(),
            observe=AsyncMock(return_value={"url": "https://example.com"}),
            list_tabs=AsyncMock(return_value=[]),
        )
        self.gateway = McpToolGateway(
            manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
        )

    async def call_as(self, operator: str | None, name: str):
        if operator is not None:
            set_current_operator(operator, source="token")
        return await self.gateway.call_tool(McpToolCallRequest(name=name, arguments={}))

    async def test_a_call_that_never_creates_does_not_wait_behind_a_slow_create(self) -> None:
        creating = asyncio.create_task(self.call_as(None, "browser.observe"))
        await asyncio.wait_for(self.creating.setdefault("None", asyncio.Event()).wait(), WAIT_SECONDS)

        listed = await asyncio.wait_for(self.call_as(None, "browser.list_tabs"), PROMPT_SECONDS)

        self.assertTrue(listed.isError)
        self.assertEqual(listed.structuredContent["code"], "no_session")
        self.assertFalse(creating.done())
        self.release["None"].set()
        self.assertFalse((await asyncio.wait_for(creating, WAIT_SECONDS)).isError)

    async def test_another_operators_create_does_not_wait_behind_a_slow_create(self) -> None:
        slow = asyncio.create_task(self.call_as("operator-a", "browser.observe"))
        await asyncio.wait_for(self.creating.setdefault("operator-a", asyncio.Event()).wait(), WAIT_SECONDS)
        self.release.setdefault("operator-b", asyncio.Event()).set()

        other = await asyncio.wait_for(self.call_as("operator-b", "browser.observe"), PROMPT_SECONDS)

        self.assertFalse(other.isError)
        self.assertEqual(self.manager.observe.await_args_list[-1].args[0], "session-operator-b")
        self.assertFalse(slow.done())
        self.release["operator-a"].set()
        self.assertFalse((await asyncio.wait_for(slow, WAIT_SECONDS)).isError)
        self.assertEqual(self.manager.create_session.await_count, 2)


if __name__ == "__main__":
    unittest.main()
