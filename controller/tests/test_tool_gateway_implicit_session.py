"""Two concurrent session-less calls create one session, not two."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models import McpToolCallRequest
from app.tool_gateway.gateway import McpToolGateway


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


if __name__ == "__main__":
    unittest.main()
