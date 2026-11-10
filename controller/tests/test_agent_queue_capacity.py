"""A full queue's refusal is its own error, so nothing else is answered as one.

The routes that queue a job caught every RuntimeError and answered `503 Service unavailable`, unlogged. The queue's
refusal at capacity was the only one on that path, but any other (an audit write that raised one after the job was
queued) then read as "try again later", with the job left queued and nothing in the log.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import agent_jobs
from app.agent_jobs import AgentJobQueue
from app.browser_manager import BrowserManager
from app.models import AgentRunRequest, McpToolCallRequest
from app.tool_gateway.gateway import McpToolGateway
from tests.test_agent_jobs import every_session_usable
from tests.test_browser_manager_create_session import _settings

AT_CAPACITY = "Job queue is at capacity. Try again later."


class FullQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store_root = Path(self.tmp.name) / "jobs"
        store_root.mkdir()
        # Never started: no worker takes the one job that fills it.
        self.queue = AgentJobQueue(
            orchestrator=SimpleNamespace(),
            store_root=store_root,
            session_accessible=every_session_usable,
            worker_count=1,
        )
        self.queue.queue = asyncio.Queue(maxsize=1)
        self.queue.queue.put_nowait("a-job-waiting")

    async def test_the_refusal_at_capacity_is_its_own_runtime_error(self) -> None:
        with self.assertRaises(RuntimeError) as refused:
            await self.queue.enqueue_run("session-1", AgentRunRequest(provider="openai", goal="g"))

        self.assertIs(type(refused.exception), agent_jobs.AgentJobQueueFull)
        self.assertEqual(str(refused.exception), AT_CAPACITY)

    async def test_the_queue_tool_at_a_full_queue_still_answers_the_queues_message(self) -> None:
        manager = BrowserManager(_settings(Path(self.tmp.name)))
        manager.get_session = AsyncMock(return_value=SimpleNamespace(id="s1"))  # type: ignore[method-assign]
        gateway = McpToolGateway(
            manager=manager,
            orchestrator=SimpleNamespace(),
            job_queue=self.queue,
            tool_profile="full",
            tool_packs="agent_jobs",
        )

        response = await gateway.call_tool(
            McpToolCallRequest(
                name="browser.queue_agent_run",
                arguments={"session_id": "s1", "request": {"provider": "openai", "goal": "g"}},
            )
        )

        self.assertTrue(response.isError)
        self.assertIn(AT_CAPACITY, response.content[0].text)


if __name__ == "__main__":
    unittest.main()
