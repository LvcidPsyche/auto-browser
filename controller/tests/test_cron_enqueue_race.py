"""A cron run's job and its session stay in step across the enqueue's last await.

`_enqueue` persists and queues the job, then awaits an audit write. A cancellation there used to close the
session while the queued job survived (and startup would re-queue it), and a worker that finished the job
during that await ran its finish callback before cron had registered one, stranding the cron slot.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.agent_jobs import AgentJobQueue
from app.cron_service import CronService
from tests.test_agent_jobs import FakeOrchestrator, every_session_usable

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class _HoldingAudit:
    """An audit store whose `agent_job_enqueued` write waits for `release` (or for the job to finish)."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.until_finished: AgentJobQueue | None = None

    async def append(self, **kwargs) -> None:
        if kwargs["event_type"] != "agent_job_enqueued":
            return
        self.reached.set()
        while self.until_finished is not None:
            records = await self.until_finished.store.list()
            if records and all(record.status in TERMINAL for record in records):
                return
            await asyncio.sleep(0.01)
        await self.release.wait()


class CronEnqueueRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.audit = _HoldingAudit()
        self.queue = AgentJobQueue(
            orchestrator=FakeOrchestrator(),
            store_root=root / "jobs",
            session_accessible=every_session_usable,
            worker_count=1,
            audit_store=self.audit,
        )
        self.manager = MagicMock()
        self.manager.create_session = AsyncMock(return_value={"id": "session-1"})
        self.manager.close_session = AsyncMock()
        self.cron = CronService(root / "crons.json", job_queue=self.queue, manager=self.manager)
        self.job = await self.cron.create_job(name="race", goal="run", webhook_enabled=True)

    async def test_a_cancellation_after_the_job_was_queued_withdraws_it_before_closing_the_session(self) -> None:
        await self.queue.store.startup()  # no workers: a queued job stays queued
        task = asyncio.create_task(self.cron.trigger_job(self.job["id"]))
        await asyncio.wait_for(self.audit.reached.wait(), timeout=5)
        self.assertEqual(len(await self.queue.store.list(status="queued")), 1)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(await self.queue.store.list(status="queued"), [], "a runnable job is left on a closed session")
        self.assertEqual([record.status for record in await self.queue.store.list()], ["cancelled"])
        self.manager.close_session.assert_awaited_once_with("session-1")
        self.assertEqual(self.cron._active_runs, {})

    async def test_a_job_that_finishes_before_enqueue_returns_still_releases_the_slot(self) -> None:
        await self.queue.startup()
        self.addAsyncCleanup(self.queue.shutdown)
        self.audit.until_finished = self.queue

        result = await asyncio.wait_for(self.cron.trigger_job(self.job["id"]), timeout=10)

        self.assertTrue(result["triggered"])
        self.assertEqual(self.cron._active_runs, {}, "the finished job's cron slot is stranded")
        self.manager.close_session.assert_awaited_once_with("session-1")
