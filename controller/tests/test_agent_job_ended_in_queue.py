"""A run that is cancelled or discarded while it waits in the queue is finished there, once.

Its finish callback (a cron run's closes the session, frees the cron job's slot and writes how the run ended) ran only
when a worker took the id off the queue: behind a long run, with one worker, that is as long as the run ahead takes.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.agent_jobs import AgentJobQueue
from app.audit import get_current_operator
from app.cron_service import CronService
from app.models import AgentRunRequest, OperatorIdentity
from app.session_ownership import acting_as
from tests.test_agent_jobs import _SlowOrchestrator, every_session_usable

ALICE = OperatorIdentity(id="alice", source="token")


def a_run(goal: str) -> AgentRunRequest:
    return AgentRunRequest(provider="openai", goal=goal, max_steps=1)


class _Finish:
    """A finish callback that records each call with the record's status then; with `hold`, it waits for `release`."""

    def __init__(self, queue: AgentJobQueue, *, hold: bool = False) -> None:
        self.queue = queue
        self.calls: list[tuple[str, str]] = []
        self.operators: list[OperatorIdentity] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.done = asyncio.Event()
        self.cancelled = False
        if not hold:
            self.release.set()

    async def __call__(self, job_id: str) -> None:
        self.calls.append((job_id, (await self.queue.store.get(job_id)).status))
        self.operators.append(get_current_operator())
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.done.set()


class EndedInTheQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orchestrator = _SlowOrchestrator()
        self.queue = AgentJobQueue(
            orchestrator=self.orchestrator,
            store_root=Path(self.tmp.name),
            session_accessible=every_session_usable,
            worker_count=1,
        )
        await self.queue.startup()
        self.addAsyncCleanup(self.queue.shutdown)
        # The one worker is busy with this run until a test ends it.
        self.ahead = await self.queue.enqueue_run("session-ahead", a_run("a long run"))
        await asyncio.wait_for(self.orchestrator.started.wait(), timeout=5)

    async def queued_behind(self, *, hold: bool = False) -> tuple[dict, _Finish]:
        finish = _Finish(self.queue, hold=hold)
        job = await self.queue.enqueue_run("session-behind", a_run("waits its turn"), on_finish=finish)
        self.assertEqual((await self.queue.store.get(job["id"])).status, "queued")
        return job, finish

    async def the_worker_reaches_everything(self) -> None:
        """End the run ahead and wait until the worker has taken every id off the queue and dealt with it."""
        await self.queue.cancel_job(self.ahead["id"])
        await asyncio.wait_for(self.queue.queue.join(), timeout=5)

    async def test_a_run_cancelled_in_the_queue_is_finished_while_the_worker_is_still_busy(self) -> None:
        job, finish = await self.queued_behind()

        answer = await self.queue.cancel_job(job["id"])

        self.assertEqual(answer["status"], "cancelled")
        self.assertEqual(finish.calls, [(job["id"], "cancelled")])
        self.assertIn(self.ahead["id"], self.queue._running_tasks, "the run ahead is over: the test shows nothing")

    async def test_a_run_discarded_in_the_queue_is_finished_while_the_worker_is_still_busy(self) -> None:
        job, finish = await self.queued_behind()

        answer = await self.queue.discard_job(job["id"])

        self.assertEqual(answer["status"], "discarded")
        self.assertEqual(finish.calls, [(job["id"], "discarded")])
        self.assertIn(self.ahead["id"], self.queue._running_tasks)

    async def test_the_worker_that_reaches_the_id_later_does_not_finish_it_again(self) -> None:
        for end in (self.queue.cancel_job, self.queue.discard_job):
            with self.subTest(end=end.__name__):
                job, finish = await self.queued_behind()
                await end(job["id"])
                self.assertEqual(len(finish.calls), 1)
        await self.the_worker_reaches_everything()

        self.assertEqual(len(finish.calls), 1)
        self.assertEqual(self.queue._on_finish, {}, "something is left registered for a job the worker has dealt with")

    async def test_a_second_cancel_and_a_discard_after_it_do_not_finish_it_again(self) -> None:
        job, finish = await self.queued_behind()

        await self.queue.cancel_job(job["id"])
        await self.queue.cancel_job(job["id"])
        await self.queue.discard_job(job["id"])
        await self.queue.discard_job(job["id"])

        self.assertEqual(finish.calls, [(job["id"], "cancelled")])

    async def test_a_shutdown_after_it_does_not_finish_it_again(self) -> None:
        job, finish = await self.queued_behind()
        await self.queue.cancel_job(job["id"])

        await self.queue.shutdown()

        self.assertEqual(finish.calls, [(job["id"], "cancelled")])

    async def test_a_run_still_queued_keeps_its_callback_for_the_worker(self) -> None:
        job, finish = await self.queued_behind()
        other, _other_finish = await self.queued_behind()

        await self.queue.cancel_job(other["id"])

        self.assertEqual(finish.calls, [])
        self.assertIn(job["id"], self.queue._on_finish)

    async def test_the_callers_cancellation_does_not_stop_the_finish(self) -> None:
        # Cancelled inside it, a cron run's close never happens, and nothing else closes the session.
        job, finish = await self.queued_behind(hold=True)
        cancel = asyncio.create_task(self.queue.cancel_job(job["id"]))
        await asyncio.wait_for(finish.entered.wait(), timeout=5)

        cancel.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancel
        self.assertFalse(finish.cancelled)
        await self.queue.cancel_job(job["id"])  # a second cancel while the first one's finish still runs
        finish.release.set()
        await asyncio.wait_for(finish.done.wait(), timeout=5)

        self.assertEqual(finish.calls, [(job["id"], "cancelled")])

    async def test_a_finish_still_running_at_shutdown_is_stopped_as_a_workers_is(self) -> None:
        job, finish = await self.queued_behind(hold=True)
        cancel = asyncio.create_task(self.queue.cancel_job(job["id"]))
        await asyncio.wait_for(finish.entered.wait(), timeout=5)

        # Timed by hand: a shutdown that waited for the finish and was cancelled by a timeout would stop the finish
        # too, and look the same from here.
        clock = asyncio.get_running_loop().time
        began = clock()
        await asyncio.wait_for(self.queue.shutdown(), timeout=5)

        self.assertLess(clock() - began, 2.0, "shutdown waited for a finish that was not going to end")
        self.assertTrue(finish.cancelled)
        self.assertEqual(self.queue._queue_ended_finishes, set())
        self.assertEqual((await asyncio.wait_for(cancel, timeout=5))["status"], "cancelled")
        self.assertEqual(len(finish.calls), 1)

    async def test_a_finish_that_fails_is_logged_and_the_cancel_still_answers(self) -> None:
        async def fails(_job_id: str) -> None:
            raise RuntimeError("the session would not close")

        job = await self.queue.enqueue_run("session-behind", a_run("waits its turn"), on_finish=fails)

        with self.assertLogs("app.agent_jobs", level="ERROR") as logged:
            answer = await self.queue.cancel_job(job["id"])

        self.assertEqual(answer["status"], "cancelled")
        self.assertTrue(any(job["id"] in line for line in logged.output), logged.output)

    async def test_the_finish_runs_as_a_workers_does_not_as_whoever_cancelled(self) -> None:
        with acting_as(ALICE):
            by_cancel, cancelled = await self.queued_behind()
            by_worker, left = await self.queued_behind()
            await self.queue.cancel_job(by_cancel["id"])
            # The other one is cancelled once the worker runs it, so its finish is the worker's.
            await self.queue.cancel_job(self.ahead["id"])
            async with asyncio.timeout(5):
                while (await self.queue.store.get(by_worker["id"])).status != "running":
                    await asyncio.sleep(0.01)
            await self.queue.cancel_job(by_worker["id"])
        await asyncio.wait_for(left.done.wait(), timeout=5)

        self.assertEqual([call[0] for call in left.calls], [by_worker["id"]])
        self.assertNotEqual(cancelled.operators[0].id, "alice")
        self.assertEqual(cancelled.operators, left.operators)


class AWorkerHoldsItTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_job_a_worker_has_taken_is_left_to_the_worker(self) -> None:
        # The worker has the id off the queue but has not started it: the cancel ends the record, and the worker,
        # finding it ended, finishes it. Finished from both sides, a cron run's session would be closed twice.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        queue = AgentJobQueue(
            orchestrator=_SlowOrchestrator(),
            store_root=Path(tmp.name),
            session_accessible=every_session_usable,
            worker_count=1,
        )
        taken, go_on = asyncio.Event(), asyncio.Event()

        async def queued_by(_job_id: str) -> None:
            taken.set()
            await go_on.wait()

        queue._queued_by = queued_by  # type: ignore[method-assign]
        await queue.startup()
        self.addAsyncCleanup(queue.shutdown)
        finish = _Finish(queue)
        job = await queue.enqueue_run("session-1", a_run("taken, not started"), on_finish=finish)
        await asyncio.wait_for(taken.wait(), timeout=5)
        self.assertIn(job["id"], queue._running_tasks)

        answer = await queue.cancel_job(job["id"])
        self.assertEqual(answer["status"], "cancelled")
        self.assertEqual(finish.calls, [])
        go_on.set()
        await asyncio.wait_for(finish.done.wait(), timeout=5)

        self.assertEqual(finish.calls, [(job["id"], "cancelled")])


class ACronRunEndedInTheQueueTests(unittest.IsolatedAsyncioTestCase):
    """What the callback is for: the session, the cron job's slot and last_status."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.orchestrator = _SlowOrchestrator()
        self.queue = AgentJobQueue(
            orchestrator=self.orchestrator,
            store_root=root / "jobs",
            session_accessible=every_session_usable,
            worker_count=1,
        )
        sessions = iter(range(1, 100))
        self.manager = MagicMock()
        self.manager.create_session = AsyncMock(side_effect=lambda **_kwargs: {"id": f"session-{next(sessions)}"})
        self.manager.close_session = AsyncMock()
        self.cron = CronService(root / "crons.json", job_queue=self.queue, manager=self.manager)
        await self.queue.startup()
        self.addAsyncCleanup(self.queue.shutdown)
        ahead = await self.cron.create_job(name="ahead", goal="a long run", webhook_enabled=True)
        self.assertTrue((await self.cron.trigger_job(ahead["id"]))["triggered"])
        await asyncio.wait_for(self.orchestrator.started.wait(), timeout=5)
        self.job = await self.cron.create_job(name="behind", goal="waits its turn", webhook_enabled=True)
        self.triggered = await self.cron.trigger_job(self.job["id"])
        self.assertEqual(self.triggered["session_id"], "session-2")

    async def test_cancelled_or_discarded_it_gives_back_its_session_and_slot_and_says_how_it_ended(self) -> None:
        for end, status in ((self.queue.cancel_job, "cancelled"), (self.queue.discard_job, "discarded")):
            with self.subTest(end=end.__name__):
                self.assertEqual(self.cron._load()[self.job["id"]]["last_status"], "queued")
                self.assertEqual(
                    (await self.cron.trigger_job(self.job["id"]))["reason"], "previous_run_active", "not in the queue"
                )
                self.manager.close_session.reset_mock()

                await end(self.triggered["queued_job"]["id"])

                self.manager.close_session.assert_awaited_once_with(self.triggered["session_id"])
                self.assertNotIn(self.job["id"], self.cron._active_runs)
                self.assertEqual(self.cron._load()[self.job["id"]]["last_status"], status)
                # The job can run again, though the worker is still busy with the run ahead.
                self.triggered = await self.cron.trigger_job(self.job["id"])
                self.assertTrue(self.triggered["triggered"])
        self.assertFalse(self.orchestrator.cancelled.is_set(), "the run ahead is over: the test shows nothing")


if __name__ == "__main__":
    unittest.main()
