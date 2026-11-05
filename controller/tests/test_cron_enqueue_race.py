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


async def _until(condition, *, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


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

    async def test_a_cancelled_trigger_whose_job_a_worker_holds_releases_the_run_once(self) -> None:
        # Withdrawing the running job fires its finish callback, which releases the run; the trigger's own
        # cleanup then runs too. It must not close the session again, nor free a slot a newer trigger took.
        running = asyncio.Event()

        class Blocking:
            async def run(self, **_kwargs):
                running.set()
                await asyncio.Event().wait()

        self.queue.orchestrator = Blocking()
        await self.queue.startup()
        self.addAsyncCleanup(self.queue.shutdown)
        job_id = self.job["id"]
        self.manager.create_session.side_effect = [{"id": "session-1"}, {"id": "session-2"}]
        newer: list[asyncio.Task] = []

        async def close_session(session_id: str) -> None:
            if session_id == "session-1" and not newer:
                # A newer trigger takes the slot as soon as this run has given it up.
                newer.append(asyncio.create_task(self.cron.trigger_job(job_id)))
                await _until(lambda: self.cron._active_runs.get(job_id) == "session-2")

        async def finish_newer() -> None:
            self.audit.release.set()
            for task in newer:
                await asyncio.wait_for(task, timeout=5)

        self.addAsyncCleanup(finish_newer)
        self.manager.close_session.side_effect = close_session

        first = asyncio.create_task(self.cron.trigger_job(job_id))
        await asyncio.wait_for(running.wait(), timeout=5)  # a worker holds the job
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first

        self.assertEqual(self.cron._active_runs, {job_id: "session-2"}, "the newer trigger's slot was freed")
        self.manager.close_session.assert_awaited_once_with("session-1")

    async def _cancel_while_queuing(self) -> str:
        """Start a trigger, cancel it while its job is queued (no workers), return the job id once it has raised."""
        await self.queue.store.startup()
        task = asyncio.create_task(self.cron.trigger_job(self.job["id"]))
        await asyncio.wait_for(self.audit.reached.wait(), timeout=5)
        [record] = await self.queue.store.list()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        return record.id

    async def test_a_withdrawal_that_fails_unexpectedly_is_logged_with_the_job_id(self) -> None:
        self.queue.cancel_job = AsyncMock(side_effect=OSError("store unavailable"))

        with self.assertLogs("app.agent_jobs", level="ERROR") as logged:
            job_id = await self._cancel_while_queuing()

        self.assertTrue(any(job_id in line for line in logged.output), logged.output)
        self.assertEqual(self.queue._on_finish, {})

    async def test_a_job_that_already_finished_is_not_withdrawn_and_nothing_is_logged(self) -> None:
        await self.queue.store.startup()
        task = asyncio.create_task(self.cron.trigger_job(self.job["id"]))
        await asyncio.wait_for(self.audit.reached.wait(), timeout=5)
        [record] = await self.queue.store.list()
        await self.queue.store.finish(record.id, status="completed", result=None, error=None)

        with self.assertNoLogs("app.agent_jobs", level="ERROR"):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual([item.status for item in await self.queue.store.list()], ["completed"])
        self.assertEqual(self.queue._on_finish, {})

    async def test_the_finish_callback_is_dropped_even_when_the_withdrawal_is_cancelled(self) -> None:
        self.queue.cancel_job = AsyncMock(side_effect=asyncio.CancelledError())

        await self._cancel_while_queuing()

        self.assertEqual(self.queue._on_finish, {}, "a callback for a session already closed is left behind")
