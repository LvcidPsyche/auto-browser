from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

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


class AgentJobQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.orchestrator = FakeOrchestrator()
        self.queue = AgentJobQueue(
            orchestrator=self.orchestrator,
            store_root=Path(self.tempdir.name),
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
            worker_count=1,
        )
        await self.queue.startup()

        updated = await self.queue.get_job(record.id)
        self.assertEqual(updated["status"], "interrupted")
        self.assertTrue(updated["resumable"])
