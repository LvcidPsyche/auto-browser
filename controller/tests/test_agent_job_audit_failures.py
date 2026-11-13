"""What an audit that cannot be written does to a job it is about.

The rule (agent_jobs.AgentJobQueue._audit_quietly): the `agent_job_enqueued` audit decides whether a job runs, and an
audit of anything after that is about something already in effect. A resume's own `agent_job_resumed` audit is written
after its job was admitted, so its failure is logged and the resume answers `queued` (A13).
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.agent_jobs import AgentJobQueue
from app.models import AgentJobCheckpoint, AgentRunRequest
from app.utils import utc_now
from tests.test_agent_jobs import FakeOrchestrator, every_session_usable

FINAL = {"completed", "failed", "interrupted", "cancelled"}


class _Audit:
    """An audit store whose appends of the event types in `failing` raise, as they do on a full disk."""

    def __init__(self, *failing: str) -> None:
        self.failing = set(failing)
        self.events: list[str] = []

    async def append(self, **kwargs) -> None:
        self.events.append(kwargs["event_type"])
        if kwargs["event_type"] in self.failing:
            raise OSError("No space left on device")


class _QueueCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.audit = _Audit()
        self.orchestrator = FakeOrchestrator()
        self.queue = AgentJobQueue(
            orchestrator=self.orchestrator,
            store_root=Path(tmp.name) / "jobs",
            session_accessible=every_session_usable,
            worker_count=1,
            audit_store=self.audit,
        )

    async def _started(self) -> None:
        await self.queue.startup()
        self.addAsyncCleanup(self.queue.shutdown)

    async def _until_final(self, job_id: str) -> dict:
        for _ in range(200):
            stored = await self.queue.get_job(job_id)
            if stored["status"] in FINAL:
                return stored
            await asyncio.sleep(0.02)
        self.fail(f"job {job_id} did not finish")

    async def _an_interrupted_run(self) -> str:
        record = await self.queue.store.create(
            session_id="session-13",
            kind="agent_run",
            request=AgentRunRequest(provider="openai", goal="finish it", max_steps=2).model_dump(),
        )
        await self.queue.store.append_checkpoint(
            record.id, AgentJobCheckpoint(step_index=1, created_at=utc_now(), status="acted")
        )
        record = await self.queue.store.get(record.id)
        record.status = "interrupted"
        await self.queue.store.update(record)
        return record.id


class ResumeAuditTests(_QueueCase):
    async def test_a_resume_whose_own_audit_fails_answers_queued_and_its_job_runs(self) -> None:
        # A13: the resume raised (REST answered 500) while the job it queued ran, and a caller who retried a "failed"
        # resume ran it twice.
        await self._started()
        source = await self._an_interrupted_run()
        self.audit.failing.add("agent_job_resumed")

        with self.assertLogs("app.agent_jobs", level="ERROR") as logged:
            resumed = await self.queue.resume_job(source)

        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["parent_job_id"], source)
        self.assertEqual((await self._until_final(resumed["id"]))["status"], "completed")
        self.assertEqual(len(self.orchestrator.run_calls), 1, "the resumed job ran once")
        self.assertTrue(any("agent_job_resumed" in line and resumed["id"] in line for line in logged.output))

    async def test_two_attempts_at_a_resume_whose_audit_fails_each_answer(self) -> None:
        await self._started()
        source = await self._an_interrupted_run()
        self.audit.failing.add("agent_job_resumed")

        with self.assertLogs("app.agent_jobs", level="ERROR"):
            first = await self.queue.resume_job(source)
            second = await self.queue.resume_job(source)

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual({first["status"], second["status"]}, {"queued"})

    async def test_a_resume_whose_audit_is_written_audits_it(self) -> None:
        await self._started()
        source = await self._an_interrupted_run()

        resumed = await self.queue.resume_job(source)

        self.assertIn("agent_job_resumed", self.audit.events)
        self.assertEqual(resumed["status"], "queued")


if __name__ == "__main__":
    unittest.main()
