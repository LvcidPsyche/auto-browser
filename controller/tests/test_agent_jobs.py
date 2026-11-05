from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.agent_jobs import AgentJobQueue
from app.models import AgentRunRequest, AgentRunResult, AgentStepRequest, AgentStepResult


class FakeOrchestrator:
    def __init__(self) -> None:
        self.run_calls: list[dict] = []

    async def step(self, **kwargs):
        await asyncio.sleep(0.01)
        return AgentStepResult(
            provider=kwargs["provider_name"],
            model="test-model",
            goal=kwargs["goal"],
            workflow_profile=kwargs.get("workflow_profile", "fast"),
            status="done",
            observation={"url": "https://example.com"},
            decision={"action": "done", "reason": "done"},
            execution=None,
            usage=None,
            raw_text=None,
            error=None,
            error_code=None,
        )

    async def run(self, **kwargs):
        self.run_calls.append(kwargs)
        await asyncio.sleep(0.01)
        steps = []
        for step_index in range(1, kwargs["max_steps"] + 1):
            step = AgentStepResult(
                provider=kwargs["provider_name"],
                model="test-model",
                goal=kwargs["goal"],
                workflow_profile=kwargs.get("workflow_profile", "fast"),
                status="acted",
                observation={"url": f"https://example.com/{step_index}", "title": f"Step {step_index}"},
                decision={"action": "click", "reason": f"step {step_index}"},
                execution={"after": {"url": f"https://example.com/{step_index}", "title": f"Step {step_index}"}},
                usage=None,
                raw_text=None,
                error=None,
                error_code=None,
            )
            steps.append(step)
            if kwargs.get("on_step"):
                await kwargs["on_step"](step_index, step)
        return AgentRunResult(
            provider=kwargs["provider_name"],
            model="test-model",
            goal=kwargs["goal"],
            workflow_profile=kwargs.get("workflow_profile", "fast"),
            status="max_steps_reached",
            steps=steps,
            final_session={"id": kwargs["session_id"], "status": "active"},
        )


async def every_session_usable(_session_id: str) -> bool:
    """No session in these tests is owned (see test_session_ownership.py)."""
    return True


class _FillingAudit:
    """An audit store whose appends fail once `full` is set, as they do on a full disk."""

    def __init__(self, *, full: bool = False) -> None:
        self.full = full

    async def append(self, **_kwargs) -> None:
        if self.full:
            raise OSError("No space left on device")


class _SlowOrchestrator:
    """A run that lasts until it is cancelled, and says when it started and when it stopped.

    Given `stop`, a cancelled run says it is `stopping` and ends only once `stop` is set.
    """

    def __init__(self, *, stop: asyncio.Event | None = None) -> None:
        self.started = asyncio.Event()
        self.stopping = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.stop = stop

    async def run(self, **_kwargs):
        self.started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.stopping.set()
            if self.stop is not None:
                await self.stop.wait()
            raise
        finally:
            self.cancelled.set()


class _HeldAudit:
    """An audit store whose appends of one event type wait until `release` is set; `held` says one is waiting."""

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        self.held = asyncio.Event()
        self.release = asyncio.Event()
        self.events: list[str] = []

    async def append(self, **kwargs) -> None:
        self.events.append(kwargs["event_type"])
        if kwargs["event_type"] == self.event_type:
            self.held.set()
            await self.release.wait()


class AgentJobQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.orchestrator = FakeOrchestrator()
        self.queue = AgentJobQueue(
            orchestrator=self.orchestrator,
            store_root=Path(self.tempdir.name),
            session_accessible=every_session_usable,
            worker_count=1,
        )
        await self.queue.startup()

    async def asyncTearDown(self) -> None:
        await self.queue.shutdown()
        self.tempdir.cleanup()

    async def test_enqueued_step_job_runs_to_completion(self) -> None:
        job = await self.queue.enqueue_step(
            "session-1",
            AgentStepRequest(provider="openai", goal="do one thing"),
        )

        for _ in range(50):
            stored = await self.queue.get_job(job["id"])
            if stored["status"] == "completed":
                break
            await asyncio.sleep(0.02)
        else:
            self.fail("step job did not complete")

        self.assertEqual(stored["result"]["status"], "done")
        self.assertEqual(stored["kind"], "agent_step")
        self.assertEqual(stored["checkpoint_count"], 1)
        self.assertFalse(stored["resumable"])

    async def test_agent_run_records_checkpoints_and_resume_queues_remaining_steps(self) -> None:
        job = await self.queue.enqueue_run(
            "session-2",
            AgentRunRequest(provider="openai", goal="run it", max_steps=3, workflow_profile="governed"),
        )

        for _ in range(50):
            stored = await self.queue.get_job(job["id"])
            if stored["status"] == "completed":
                break
            await asyncio.sleep(0.02)
        else:
            self.fail("run job did not complete")

        self.assertEqual(stored["checkpoint_count"], 3)
        self.assertTrue(stored["resumable"])
        self.assertEqual(stored["checkpoints"][-1]["url"], "https://example.com/3")
        self.assertEqual(stored["result"]["workflow_profile"], "governed")

        resumed = await self.queue.resume_job(job["id"])
        resumed_record = await self.queue.get_job(resumed["id"])

        self.assertEqual(resumed_record["parent_job_id"], job["id"])
        self.assertEqual(resumed_record["request"]["workflow_profile"], "governed")
        self.assertEqual(resumed_record["request"]["max_steps"], 1)
        self.assertIn("Resuming background agent job", resumed_record["request"]["context_hints"])
        self.assertIn("https://example.com/3", resumed_record["request"]["context_hints"])

    async def test_resume_context_stays_within_the_request_limit(self) -> None:
        # Long checkpoint URLs, operator hints and repeated resumes each pushed
        # context_hints past its limit, and the resumed job then failed with a
        # ValidationError before running a single step.
        long_url = "https://example.com/" + "a" * 1900

        async def long_url_run(**kwargs):
            for step_index in range(1, kwargs["max_steps"] + 1):
                step = AgentStepResult(
                    provider=kwargs["provider_name"],
                    model="test-model",
                    goal=kwargs["goal"],
                    status="acted",
                    observation={"url": long_url},
                    decision={"action": "click", "reason": "r"},
                    execution={"after": {"url": long_url}},
                )
                await kwargs["on_step"](step_index, step)
            return AgentRunResult(
                provider=kwargs["provider_name"],
                model="test-model",
                goal=kwargs["goal"],
                status="max_steps_reached",
                steps=[],
                final_session={"id": kwargs["session_id"]},
            )

        self.orchestrator.run = long_url_run
        job = await self.queue.enqueue_run(
            "session-9",
            AgentRunRequest(provider="openai", goal="go", max_steps=6, context_hints="stay on site " * 300),
        )
        for _ in range(3):
            for _ in range(100):
                stored = await self.queue.get_job(job["id"])
                if stored["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(stored["status"], "completed", stored["error"])
            job = await self.queue.resume_job(job["id"], max_steps=6)

        hints = job["request"]["context_hints"]
        self.assertLessEqual(len(hints), 4000)
        self.assertTrue(hints.startswith("stay on site"))
        self.assertEqual(hints.count("Resuming background agent job"), 1)
        AgentRunRequest.model_validate(job["request"])

    async def test_restart_settles_a_job_that_was_being_cancelled(self) -> None:
        record = await self.queue.store.create(
            session_id="session-4",
            kind="agent_run",
            request=AgentRunRequest(provider="openai", goal="stale work").model_dump(),
        )
        record.status = "cancelling"
        await self.queue.store.update(record)

        await self.queue.store.mark_running_interrupted()

        stored = await self.queue.get_job(record.id)
        self.assertEqual(stored["status"], "cancelled")
        discarded = await self.queue.discard_job(record.id)
        self.assertEqual(discarded["status"], "discarded")

    async def test_discard_queued_job_marks_it_discarded(self) -> None:
        record = await self.queue.store.create(
            session_id="session-3",
            kind="agent_run",
            request=AgentRunRequest(provider="openai", goal="stale work").model_dump(),
        )

        discarded = await self.queue.discard_job(record.id)
        listed = await self.queue.list_jobs(status="discarded")

        self.assertEqual(discarded["status"], "discarded")
        self.assertEqual(discarded["error"], "agent_job_discarded")
        self.assertFalse(discarded["resumable"])
        self.assertEqual([item["id"] for item in listed], [record.id])

    async def test_discard_running_job_is_rejected(self) -> None:
        record = await self.queue.store.create(
            session_id="session-4",
            kind="agent_run",
            request=AgentRunRequest(provider="openai", goal="active work").model_dump(),
        )
        record.status = "running"
        await self.queue.store.update(record)

        with self.assertRaisesRegex(ValueError, "Running jobs must be cancelled"):
            await self.queue.discard_job(record.id)

        stored = await self.queue.get_job(record.id)
        self.assertEqual(stored["status"], "running")

    async def test_cancel_queued_job_marks_it_cancelled(self) -> None:
        record = await self.queue.store.create(
            session_id="session-5",
            kind="agent_run",
            request=AgentRunRequest(provider="openai", goal="cancel queued").model_dump(),
        )

        cancelled = await self.queue.cancel_job(record.id)
        listed = await self.queue.list_jobs(status="cancelled")

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["error"], "agent_job_cancelled")
        self.assertFalse(cancelled["resumable"])
        self.assertEqual([item["id"] for item in listed], [record.id])

    async def test_cancel_running_job_stops_worker_task(self) -> None:
        class SlowOrchestrator:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.cancelled = asyncio.Event()

            async def run(self, **kwargs):
                self.started.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    self.cancelled.set()

        await self.queue.shutdown()
        slow_orchestrator = SlowOrchestrator()
        self.queue = AgentJobQueue(
            orchestrator=slow_orchestrator,
            store_root=Path(self.tempdir.name),
            session_accessible=every_session_usable,
            worker_count=1,
        )
        await self.queue.startup()

        job = await self.queue.enqueue_run(
            "session-6",
            AgentRunRequest(provider="openai", goal="cancel active work"),
        )
        await asyncio.wait_for(slow_orchestrator.started.wait(), timeout=2)

        cancelled = await self.queue.cancel_job(job["id"])

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["error"], "agent_job_cancelled")
        self.assertTrue(slow_orchestrator.cancelled.is_set())

    async def _restart_with(self, orchestrator, audit) -> None:
        """Replace the queue with one that runs jobs on *orchestrator* and audits them to *audit*."""
        await self.queue.shutdown()
        self.queue = AgentJobQueue(
            orchestrator=orchestrator,
            store_root=Path(self.tempdir.name),
            session_accessible=every_session_usable,
            worker_count=1,
            audit_store=audit,
        )
        await self.queue.startup()

    async def _a_running_job_as_the_disk_fills(self) -> tuple[dict, _SlowOrchestrator, _FillingAudit, AsyncMock]:
        """A job a worker is running, with a finish callback, once every audit append has begun to fail."""
        orchestrator, audit, on_finish = _SlowOrchestrator(), _FillingAudit(), AsyncMock()
        await self._restart_with(orchestrator, audit)
        job = await self.queue.enqueue_run(
            "session-8", AgentRunRequest(provider="openai", goal="run as the disk fills"), on_finish=on_finish
        )
        await asyncio.wait_for(orchestrator.started.wait(), timeout=2)
        audit.full = True
        return job, orchestrator, audit, on_finish

    async def test_cancelling_a_running_job_stops_it_even_when_no_audit_can_be_written(self) -> None:
        # The record says cancelling before the audit is written; an audit that fails must not leave it running.
        job, orchestrator, audit, on_finish = await self._a_running_job_as_the_disk_fills()

        try:
            with self.assertLogs("app.agent_jobs", level="ERROR") as logged:
                cancelled = await self.queue.cancel_job(job["id"])
        finally:
            audit.full = False  # so a regression fails here rather than hanging the teardown's shutdown

        self.assertTrue(orchestrator.cancelled.is_set(), "the job kept running")
        self.assertEqual((cancelled["status"], cancelled["error"]), ("cancelled", "agent_job_cancelled"))
        self.assertTrue(any(job["id"] in line for line in logged.output), logged.output)
        on_finish.assert_awaited_once()

    async def test_shutdown_stops_a_running_job_even_when_no_audit_can_be_written(self) -> None:
        # A failing interruption audit replaced the job's CancelledError, so its worker swallowed the shutdown's
        # cancel and went on serving the queue, and shutdown waited for it forever.
        job, orchestrator, _audit, on_finish = await self._a_running_job_as_the_disk_fills()

        with self.assertLogs("app.agent_jobs", level="ERROR"):
            shutdown = asyncio.create_task(self.queue.shutdown())
            done, _ = await asyncio.wait({shutdown}, timeout=5)  # asyncio.wait cancels nothing on its timeout
        if not done:
            shutdown.cancel()  # a second cancel stops the worker that swallowed the first, so the test can end
            await asyncio.wait({shutdown}, timeout=5)

        self.assertIn(shutdown, done, "shutdown is still waiting for a worker that swallowed its cancel")
        self.assertTrue(orchestrator.cancelled.is_set())
        self.assertEqual((await self.queue.store.get(job["id"])).status, "interrupted")
        on_finish.assert_awaited_once()

    async def test_shutdown_stops_a_running_job_even_when_its_record_cannot_be_written(self) -> None:
        # On a full disk the job's own record fails first as it is interrupted. That error took the place of the
        # cancellation, so its worker took the shutdown for a crashed job and went on serving the queue.
        _job, orchestrator, _audit, on_finish = await self._a_running_job_as_the_disk_fills()
        self.queue.store.finish_cancelled = AsyncMock(side_effect=OSError("No space left on device"))  # type: ignore[method-assign]

        with self.assertLogs("app.agent_jobs", level="ERROR"):
            shutdown = asyncio.create_task(self.queue.shutdown())
            done, _ = await asyncio.wait({shutdown}, timeout=5)  # asyncio.wait cancels nothing on its timeout
        if not done:
            shutdown.cancel()  # a second cancel stops the worker that swallowed the first, so the test can end
            await asyncio.wait({shutdown}, timeout=5)

        self.assertIn(shutdown, done, "shutdown is still waiting for a worker that swallowed its cancel")
        self.assertTrue(orchestrator.cancelled.is_set())
        on_finish.assert_awaited_once()

    async def test_a_job_cancelled_as_it_starts_ends_cancelled(self) -> None:
        # Stopped while its "started" audit was being written, the job skipped its cancellation handling: the record
        # stayed "cancelling", read as live and could not be discarded until the next restart.
        audit, on_finish = _HeldAudit("agent_job_started"), AsyncMock()
        await self._restart_with(self.orchestrator, audit)
        job = await self.queue.enqueue_run(
            "session-11", AgentRunRequest(provider="openai", goal="cancel as it starts"), on_finish=on_finish
        )
        await asyncio.wait_for(audit.held.wait(), timeout=2)

        cancelled = await self.queue.cancel_job(job["id"])

        self.assertEqual((cancelled["status"], cancelled["error"]), ("cancelled", "agent_job_cancelled"))
        self.assertIn("agent_job_cancelled", audit.events)
        on_finish.assert_awaited_once()

    async def test_a_cancel_stops_the_job_before_its_own_audit_is_written(self) -> None:
        # The job used to be stopped after the cancel_requested audit: a slow audit write kept it running meanwhile,
        # and a failed one kept it running for good.
        orchestrator, audit = _SlowOrchestrator(), _HeldAudit("agent_job_cancel_requested")
        await self._restart_with(orchestrator, audit)
        job = await self.queue.enqueue_run("session-12", AgentRunRequest(provider="openai", goal="cancel, slow audit"))
        await asyncio.wait_for(orchestrator.started.wait(), timeout=2)

        cancelling = asyncio.create_task(self.queue.cancel_job(job["id"]))
        await asyncio.wait_for(audit.held.wait(), timeout=2)
        try:
            await asyncio.wait_for(orchestrator.cancelled.wait(), timeout=2)
        except TimeoutError:
            self.fail("the job kept running while its cancel's audit was being written")
        finally:
            audit.release.set()

        self.assertEqual((await cancelling)["status"], "cancelled")

    async def test_cancelling_the_cancel_call_itself_is_not_swallowed(self) -> None:
        # wait_for(shield(task)) inside `except CancelledError: pass` swallowed a cancellation of the caller (a client
        # that went away, a shutdown), which then carried on as if nothing had happened.
        orchestrator = _SlowOrchestrator(stop=asyncio.Event())
        await self._restart_with(orchestrator, None)
        job = await self.queue.enqueue_run("session-13", AgentRunRequest(provider="openai", goal="slow to stop"))
        await asyncio.wait_for(orchestrator.started.wait(), timeout=2)
        running = self.queue._running_tasks[job["id"]]

        caller = asyncio.create_task(self.queue.cancel_job(job["id"]))
        await asyncio.wait_for(orchestrator.stopping.wait(), timeout=2)
        caller.cancel()
        done, _ = await asyncio.wait({caller}, timeout=2)
        orchestrator.stop.set()
        await asyncio.wait({running}, timeout=2)  # its record is written before the test's folder is removed

        self.assertIn(caller, done)
        self.assertTrue(caller.cancelled(), "the cancel call swallowed its own cancellation")

    async def test_cancelling_a_queued_job_with_no_audit_written_still_cancels_it(self) -> None:
        record = await self.queue.store.create(
            session_id="session-9",
            kind="agent_run",
            request=AgentRunRequest(provider="openai", goal="cancel queued on a full disk").model_dump(),
        )
        self.queue.audit = _FillingAudit(full=True)

        with self.assertLogs("app.agent_jobs", level="ERROR") as logged:
            cancelled = await self.queue.cancel_job(record.id)

        self.assertEqual((cancelled["status"], cancelled["error"]), ("cancelled", "agent_job_cancelled"))
        self.assertTrue(any(record.id in line for line in logged.output), logged.output)

    async def test_cancelling_a_running_record_with_no_worker_and_no_audit_written_still_interrupts_it(self) -> None:
        record = await self.queue.store.create(
            session_id="session-10",
            kind="agent_run",
            request=AgentRunRequest(provider="openai", goal="no worker holds it").model_dump(),
        )
        record.status = "running"
        await self.queue.store.update(record)
        self.queue.audit = _FillingAudit(full=True)

        with self.assertLogs("app.agent_jobs", level="ERROR") as logged:
            interrupted = await self.queue.cancel_job(record.id)

        self.assertEqual((interrupted["status"], interrupted["error"]), ("interrupted", "agent_job_interrupted"))
        self.assertTrue(any(record.id in line for line in logged.output), logged.output)

    async def test_shutdown_interrupts_running_job(self) -> None:
        class SlowOrchestrator:
            def __init__(self) -> None:
                self.started = asyncio.Event()

            async def run(self, **kwargs):
                self.started.set()
                await asyncio.sleep(60)

        await self.queue.shutdown()
        slow_orchestrator = SlowOrchestrator()
        self.queue = AgentJobQueue(
            orchestrator=slow_orchestrator,
            store_root=Path(self.tempdir.name),
            session_accessible=every_session_usable,
            worker_count=1,
        )
        await self.queue.startup()

        job = await self.queue.enqueue_run(
            "session-7",
            AgentRunRequest(provider="openai", goal="interrupt active work"),
        )
        await asyncio.wait_for(slow_orchestrator.started.wait(), timeout=2)
        await self.queue.shutdown()

        interrupted = await self.queue.get_job(job["id"])
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["error"], "agent_job_interrupted")
        self.assertTrue(interrupted["resumable"])

    async def test_running_jobs_become_interrupted_on_restart(self) -> None:
        await self.queue.store.create(
            session_id="session-2",
            kind="agent_run",
            request=AgentRunRequest(provider="openai", goal="run it").model_dump(),
        )
        records = await self.queue.store.list()
        record = records[0]
        record.status = "running"
        await self.queue.store.update(record)

        await self.queue.shutdown()
        self.queue = AgentJobQueue(
            orchestrator=FakeOrchestrator(),
            store_root=Path(self.tempdir.name),
            session_accessible=every_session_usable,
            worker_count=1,
        )
        await self.queue.startup()

        updated = await self.queue.get_job(record.id)
        self.assertEqual(updated["status"], "interrupted")
        self.assertTrue(updated["resumable"])


class _InstantOrchestrator:
    """A run that finishes at once and records the goals in the order it ran them."""

    def __init__(self) -> None:
        self.goals: list[str] = []

    async def run(self, **kwargs):
        self.goals.append(kwargs["goal"])
        return AgentRunResult(
            provider=kwargs["provider_name"],
            model="test-model",
            goal=kwargs["goal"],
            workflow_profile=kwargs.get("workflow_profile", "fast"),
            status="done",
            steps=[],
            final_session={"id": kwargs["session_id"], "status": "active"},
        )


class AgentJobQueueBacklogTests(unittest.IsolatedAsyncioTestCase):
    """More queued records on disk than the queue holds must not stall startup (it blocked on record 101)."""

    BACKLOG = 150

    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.orchestrator = _InstantOrchestrator()
        self.queue = AgentJobQueue(
            orchestrator=self.orchestrator,
            store_root=Path(self.tempdir.name),
            session_accessible=every_session_usable,
            worker_count=1,
        )
        await self.queue.store.startup()
        for index in range(self.BACKLOG):
            await self.queue.store.create(
                session_id="session-backlog",
                kind="agent_run",
                request=AgentRunRequest(provider="openai", goal=f"goal-{index:03d}").model_dump(),
                job_id=f"job-{index:03d}",
            )

    async def asyncTearDown(self) -> None:
        await self.queue.shutdown()
        self.tempdir.cleanup()

    async def test_startup_returns_at_once_and_every_queued_record_runs_in_order(self) -> None:
        expected = [record.request["goal"] for record in reversed(await self.queue.store.list(status="queued"))]
        self.assertEqual(len(expected), self.BACKLOG)

        await asyncio.wait_for(self.queue.startup(), timeout=5)
        await asyncio.wait_for(self._all_done(), timeout=30)

        self.assertEqual(self.orchestrator.goals, expected)
        self.assertEqual(await self.queue.store.list(status="queued"), [])

    async def test_shutdown_during_the_requeue_ends_promptly(self) -> None:
        # Workers that never take a job keep the queue full, so the re-queue is still waiting for room.
        self.queue._worker_loop = self._idle_worker  # type: ignore[method-assign]

        await asyncio.wait_for(self.queue.startup(), timeout=5)
        await asyncio.sleep(0.05)
        self.assertTrue(self.queue.queue.full())

        requeue = self.queue._requeue_task
        await asyncio.wait_for(self.queue.shutdown(), timeout=5)

        self.assertIsNotNone(requeue)
        self.assertTrue(requeue.cancelled())
        self.assertIsNone(self.queue._requeue_task)

    async def test_a_failed_requeue_is_logged_and_shutdown_still_stops_the_workers(self) -> None:
        self.queue._worker_loop = self._idle_worker  # type: ignore[method-assign]
        self.queue._requeue = AsyncMock(side_effect=RuntimeError("queue broke"))  # type: ignore[method-assign]
        await self.queue.startup()
        workers = list(self.queue._workers)
        await asyncio.wait([self.queue._requeue_task], timeout=5)

        with self.assertLogs("app.agent_jobs", level="ERROR") as logs:
            await asyncio.wait_for(self.queue.shutdown(), timeout=5)

        self.assertIn("queue broke", "\n".join(logs.output))
        self.assertTrue(all(worker.cancelled() for worker in workers))
        self.assertIsNone(self.queue._requeue_task)

    @staticmethod
    async def _idle_worker(_index: int) -> None:
        await asyncio.sleep(60)

    async def _all_done(self) -> None:
        while len(self.orchestrator.goals) < self.BACKLOG or await self.queue.store.list(status="running"):
            await asyncio.sleep(0.01)
