"""A cron run's job and its session stay in step across the enqueue's last await.

`_enqueue` persists and queues the job, then awaits an audit write. A cancellation there used to close the
session while the queued job survived (and startup would re-queue it), and a worker that finished the job
during that await ran its finish callback before cron had registered one, stranding the cron slot.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
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
        self.events: list[str] = []
        self.failing: set[str] = set()

    async def append(self, **kwargs) -> None:
        self.events.append(kwargs["event_type"])
        if kwargs["event_type"] in self.failing:
            raise OSError("No space left on device")
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

    def _statuses_at_close(self) -> dict[str, list[str]]:
        """As each session is closed, record the stored status of the jobs that ran in it."""
        seen: dict[str, list[str]] = {}

        async def close_session(session_id: str) -> None:
            seen[session_id] = [record.status for record in await self.queue.store.list(session_id=session_id)]

        self.manager.close_session.side_effect = close_session
        return seen

    async def test_a_cancellation_after_the_job_was_queued_withdraws_it_before_closing_the_session(self) -> None:
        await self.queue.store.startup()  # no workers: a queued job stays queued
        at_close = self._statuses_at_close()
        task = asyncio.create_task(self.cron.trigger_job(self.job["id"]))
        await asyncio.wait_for(self.audit.reached.wait(), timeout=5)
        self.assertEqual(len(await self.queue.store.list(status="queued")), 1)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(await self.queue.store.list(status="queued"), [], "a runnable job is left on a closed session")
        # Nobody cancelled it: the withdrawal is recorded as an interruption, not as an operator's cancel.
        [record] = await self.queue.store.list()
        self.assertEqual((record.status, record.error), ("interrupted", "agent_job_interrupted"))
        self._assert_withdrawn_as_an_interruption()
        self.manager.close_session.assert_awaited_once_with("session-1")
        self.assertEqual(at_close, {"session-1": ["interrupted"]}, "the session closed before its job was withdrawn")
        self.assertEqual(self.cron._active_runs, {})

    def _assert_withdrawn_as_an_interruption(self) -> None:
        self.assertIn("agent_job_interrupted", self.audit.events)
        self.assertNotIn("agent_job_cancel_requested", self.audit.events)
        self.assertNotIn("agent_job_cancelled", self.audit.events)

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
        at_close: dict[str, list[str]] = {}

        async def close_session(session_id: str) -> None:
            at_close[session_id] = [record.status for record in await self.queue.store.list(session_id=session_id)]
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
        self.assertEqual(at_close["session-1"], ["interrupted"], "the session closed before its job was withdrawn")
        self._assert_withdrawn_as_an_interruption()

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
        self.queue._withdraw = AsyncMock(side_effect=OSError("store unavailable"))

        with self.assertLogs("app.agent_jobs", level="ERROR") as logged:
            job_id = await self._cancel_while_queuing()

        self.assertTrue(any(job_id in line for line in logged.output), logged.output)
        self.assertEqual(self.queue._on_finish, {})

    async def test_a_withdrawal_whose_audit_cannot_be_written_is_still_a_withdrawal(self) -> None:
        self.audit.failing.add("agent_job_interrupted")

        with self.assertLogs("app.agent_jobs", level="ERROR") as logged:
            job_id = await self._cancel_while_queuing()

        self.assertEqual([record.status for record in await self.queue.store.list()], ["interrupted"])
        self.assertTrue(any("audit" in line and job_id in line for line in logged.output), logged.output)
        self.assertFalse(any("could not be withdrawn" in line for line in logged.output), logged.output)

    async def test_a_withdrawal_that_cannot_read_the_job_is_logged_not_taken_for_a_finished_job(self) -> None:
        # A record that does not parse raises pydantic's ValidationError, a ValueError, which the cleanup took for
        # "already finished" and dropped without a word.
        await self.queue.store.startup()
        task = asyncio.create_task(self.cron.trigger_job(self.job["id"]))
        await asyncio.wait_for(self.audit.reached.wait(), timeout=5)
        [path] = self.queue.store.root.glob("*.json")
        path.write_text("{", encoding="utf-8")

        with self.assertLogs("app.agent_jobs", level="ERROR") as logged:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(
            any("could not be withdrawn" in line and path.stem in line for line in logged.output), logged.output
        )

    async def test_a_withdrawal_leaves_a_finished_job_to_close_its_session(self) -> None:
        # The job had finished and its finish callback was closing the session when the trigger was cancelled. The
        # withdrawal cancelled that close, and the run's slot was already free, so nothing closed the session.
        closed = await self._withdraw_while_the_finished_job_closes_its_session()

        self.assertEqual(closed, ["session-1"], "the finished job's session was never closed")

    async def test_a_withdrawal_leaves_a_job_whose_end_was_not_recorded_to_close_its_session(self) -> None:
        # The write of the job's end failed (a full disk), so its record still said running and the withdrawal took it
        # for a running job: the close in its finish callback was cancelled all the same.
        self.queue.store.finish = AsyncMock(side_effect=OSError("No space left on device"))  # type: ignore[method-assign]

        closed = await self._withdraw_while_the_finished_job_closes_its_session()

        self.assertEqual(closed, ["session-1"], "the session of a job whose end was not recorded was never closed")

    async def _withdraw_while_the_finished_job_closes_its_session(self) -> list[str]:
        """Cancel a trigger while the job it queued, done running, closes its session; return the sessions closed."""
        closing, close = asyncio.Event(), asyncio.Event()
        closed: list[str] = []

        async def close_session(session_id: str) -> None:
            closing.set()
            await close.wait()
            closed.append(session_id)

        self.manager.close_session.side_effect = close_session
        await self.queue.startup()
        self.addAsyncCleanup(self.queue.shutdown)
        task = asyncio.create_task(self.cron.trigger_job(self.job["id"]))
        await asyncio.wait_for(closing.wait(), timeout=5)

        task.cancel()
        await asyncio.wait([task], timeout=0.5)
        close.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await _until(lambda: closed or not self.queue._running_tasks)
        return closed

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

    async def test_a_cancellation_while_the_job_record_is_written_leaves_no_queued_job(self) -> None:
        await self._cancel_while_the_record_is_written(times=1)

    async def test_a_second_cancellation_while_the_job_record_is_written_still_leaves_no_queued_job(self) -> None:
        # The wait for the write ended at the next cancellation, which let go of the store's lock before the record
        # landed: the withdrawal found no record, and the record then landed queued.
        await self._cancel_while_the_record_is_written(times=2)

    async def test_a_record_write_that_fails_after_a_cancellation_is_logged(self) -> None:
        # Nothing awaited the write's outcome once the call was cancelled, so its error surfaced, if ever, as
        # asyncio's "Task exception was never retrieved" when the task was collected.
        await self.queue.store.startup()  # no workers
        writing, proceed = threading.Event(), threading.Event()

        def failing_write(_record) -> None:
            writing.set()
            proceed.wait(5)
            raise OSError("No space left on device")

        self.queue.store._write_sync = failing_write  # type: ignore[method-assign]
        task = asyncio.create_task(self.cron.trigger_job(self.job["id"]))
        self.assertTrue(await asyncio.to_thread(writing.wait, 5))
        with self.assertLogs("app.agent_jobs", level="ERROR") as logged:
            task.cancel()
            await asyncio.wait([task], timeout=0.2)
            proceed.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(any("could not be written" in line for line in logged.output), logged.output)
        self.assertEqual(await self.queue.store.list(), [])
        self.manager.close_session.assert_awaited_once_with("session-1")

    async def _cancel_while_the_record_is_written(self, *, times: int) -> None:
        # to_thread cannot stop a write that has begun, so the record lands after the cancellation. Left queued,
        # nothing would withdraw it, and the next startup would run it on a session already closed.
        await self.queue.store.startup()  # no workers
        writing, proceed, written = threading.Event(), threading.Event(), threading.Event()
        write = self.queue.store._write_sync

        def slow_write(record) -> None:
            if record.status == "queued":
                writing.set()
                proceed.wait(5)
            write(record)
            written.set()

        self.queue.store._write_sync = slow_write  # type: ignore[method-assign]
        task = asyncio.create_task(self.cron.trigger_job(self.job["id"]))
        self.assertTrue(await asyncio.to_thread(writing.wait, 5))
        for _ in range(times):
            task.cancel()
            await asyncio.wait([task], timeout=0.2)  # the cleanup runs as far as it can before the write lands
        proceed.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(await asyncio.to_thread(written.wait, 5))

        self.manager.close_session.assert_awaited_once_with("session-1")
        self.assertEqual([record.status for record in await self.queue.store.list()], ["interrupted"])
        self._assert_withdrawn_as_an_interruption()
        restarted = AgentJobQueue(
            orchestrator=FakeOrchestrator(),
            store_root=self.queue.store.root,
            session_accessible=every_session_usable,
        )
        await restarted.startup()
        self.addAsyncCleanup(restarted.shutdown)
        self.assertEqual(restarted.queue.qsize(), 0, "startup put the abandoned job back on the queue")

    async def test_the_finish_callback_is_dropped_even_when_the_withdrawal_is_cancelled(self) -> None:
        self.queue._withdraw = AsyncMock(side_effect=asyncio.CancelledError())

        await self._cancel_while_queuing()

        self.assertEqual(self.queue._on_finish, {}, "a callback for a session already closed is left behind")
