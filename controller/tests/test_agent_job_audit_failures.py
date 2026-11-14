"""What an audit that cannot be written does to a job it is about.

The rule (agent_jobs.AgentJobQueue._audit_quietly): the `agent_job_enqueued` audit decides whether a job runs, and an
audit of anything after that is about something already in effect. A resume's own `agent_job_resumed` audit is written
after its job was admitted, so its failure is logged and the resume answers `queued` (A13). The admission audit's own
failure, or a caller that is cancelled while it is written, withdraws the job for every caller: a cron run's (which
always did), a REST caller's, an MCP caller's and a resume's (A15).
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from app.agent_jobs import AgentJobQueue, AgentJobStore
from app.browser_manager import BrowserManager
from app.models import AgentJobCheckpoint, AgentRunRequest, AgentStepRequest, McpToolCallRequest
from app.routes.agent import create_agent_router
from app.tool_gateway.gateway import McpToolGateway
from app.utils import utc_now
from tests.test_agent_jobs import FakeOrchestrator, _SlowOrchestrator, every_session_usable
from tests.test_browser_manager_create_session import _settings

FINAL = {"completed", "failed", "interrupted", "cancelled"}
REQUEST = AgentRunRequest(provider="openai", goal="withdrawn when its admission is not written", max_steps=1)
STEP_REQUEST = AgentStepRequest(provider="openai", goal="withdrawn when its admission is not written")


class _Audit:
    """An audit store whose appends of the event types in `failing` raise, as they do on a full disk.

    With `hold`, the `agent_job_enqueued` append waits for `release` first (`reached` says it is waiting), and then
    raises if `fail_after_hold` is set.
    """

    def __init__(self, *failing: str) -> None:
        self.failing = set(failing)
        self.events: list[str] = []
        self.hold = False
        self.fail_after_hold = False
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def append(self, **kwargs) -> None:
        event = kwargs["event_type"]
        self.events.append(event)
        if event == "agent_job_enqueued" and self.hold:
            self.reached.set()
            await self.release.wait()
            if self.fail_after_hold:
                raise OSError("No space left on device")
            return
        if event in self.failing:
            raise OSError("No space left on device")


async def _an_interrupted_run(queue: AgentJobQueue) -> str:
    record = await queue.store.create(
        session_id="session-15",
        kind="agent_run",
        request=AgentRunRequest(provider="openai", goal="finish it", max_steps=2).model_dump(),
    )
    await queue.store.append_checkpoint(
        record.id, AgentJobCheckpoint(step_index=1, created_at=utc_now(), status="acted")
    )
    record = await queue.store.get(record.id)
    record.status = "interrupted"
    await queue.store.update(record)
    return record.id


class _QueueCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.audit = _Audit()
        self.orchestrator = FakeOrchestrator()
        self.queue = self._new_queue("jobs")

    def _new_queue(self, name: str, *, audit: _Audit | None = None, orchestrator=None) -> AgentJobQueue:
        return AgentJobQueue(
            orchestrator=orchestrator or self.orchestrator,
            store_root=self.root / name,
            session_accessible=every_session_usable,
            worker_count=1,
            audit_store=audit or self.audit,
        )

    async def _started(self, queue: AgentJobQueue | None = None) -> None:
        queue = queue or self.queue
        await queue.startup()
        self.addAsyncCleanup(queue.shutdown)

    async def _until_final(self, job_id: str, queue: AgentJobQueue | None = None) -> dict:
        queue = queue or self.queue
        for _ in range(200):
            stored = await queue.get_job(job_id)
            if stored["status"] in FINAL:
                return stored
            await asyncio.sleep(0.02)
        self.fail(f"job {job_id} did not finish")


class ResumeAuditTests(_QueueCase):
    async def test_a_resume_whose_own_audit_fails_answers_queued_and_its_job_runs(self) -> None:
        # A13: the resume raised (REST answered 500) while the job it queued ran, and a caller who retried a "failed"
        # resume ran it twice.
        await self._started()
        source = await _an_interrupted_run(self.queue)
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
        source = await _an_interrupted_run(self.queue)
        self.audit.failing.add("agent_job_resumed")

        with self.assertLogs("app.agent_jobs", level="ERROR"):
            first = await self.queue.resume_job(source)
            second = await self.queue.resume_job(source)

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual({first["status"], second["status"]}, {"queued"})

    async def test_a_resume_whose_audit_is_written_audits_it(self) -> None:
        await self._started()
        source = await _an_interrupted_run(self.queue)

        resumed = await self.queue.resume_job(source)

        self.assertIn("agent_job_resumed", self.audit.events)
        self.assertEqual(resumed["status"], "queued")


# The callers that pass no callback: the REST routes and the MCP queue tools call enqueue_step and enqueue_run, and a
# resume queues its job on its own account. Each is made ready (a resume needs a source) and returns what to call.
CALLERS = ("enqueue_run", "enqueue_step", "resume")


async def _caller(queue: AgentJobQueue, name: str) -> Callable[[], Awaitable[dict]]:
    if name == "resume":
        source = await _an_interrupted_run(queue)
        return lambda: queue.resume_job(source)
    if name == "enqueue_step":
        return lambda: queue.enqueue_step("session-15", STEP_REQUEST)
    return lambda: queue.enqueue_run("session-15", REQUEST)


async def _the_job_it_made(queue: AgentJobQueue, name: str) -> dict:
    """The record the caller made: the only one, or for a resume the one with a parent."""
    records = [record for record in await queue.store.list() if name != "resume" or record.parent_job_id is not None]
    assert len(records) == 1, [record.model_dump() for record in records]
    return records[0].model_dump()


class EnqueueAuditTests(_QueueCase):
    """A15. No worker is started unless a test says so, so a job that is not withdrawn stays queued, and "none runs"
    means that nothing took it."""

    async def _ready(self, name: str, *, audit: _Audit) -> tuple[AgentJobQueue, Callable[[], Awaitable[dict]]]:
        queue = self._new_queue(name, audit=audit)
        await queue.store.startup()
        return queue, await _caller(queue, name)

    def _assert_withdrawn(self, job: dict, audit: _Audit) -> None:
        self.assertEqual((job["status"], job["error"]), ("interrupted", "agent_job_interrupted"))
        # Nobody cancelled it: an interruption, as a shutdown or a cron run's withdrawal records one.
        self.assertIn("agent_job_interrupted", audit.events)
        self.assertNotIn("agent_job_cancelled", audit.events)
        self.assertEqual(self.orchestrator.run_calls, [], "a withdrawn job ran")

    async def test_an_audit_that_fails_withdraws_the_job_and_raises(self) -> None:
        for name in CALLERS:
            with self.subTest(caller=name):
                audit = _Audit("agent_job_enqueued")
                queue, call = await self._ready(name, audit=audit)

                with self.assertRaises(OSError):
                    await call()

                self._assert_withdrawn(await _the_job_it_made(queue, name), audit)

    async def test_a_caller_cancelled_during_the_audit_withdraws_the_job(self) -> None:
        for name in CALLERS:
            with self.subTest(caller=name):
                audit = _Audit()
                audit.hold = True
                queue, call = await self._ready(name, audit=audit)
                task = asyncio.create_task(call())
                await asyncio.wait_for(audit.reached.wait(), timeout=5)
                self.assertEqual(len(await queue.store.list(status="queued")), 1, "the job waits in the queue")

                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

                self._assert_withdrawn(await _the_job_it_made(queue, name), audit)

    async def test_a_caller_cancelled_while_its_record_is_written_withdraws_the_job(self) -> None:
        # Already so before A15, for every caller; held here so that the three cases come out alike.
        for name in CALLERS:
            with self.subTest(caller=name):
                audit = _Audit()
                queue, call = await self._ready(name, audit=audit)
                blocked, release = threading.Event(), threading.Event()
                real_write = AgentJobStore._write_sync

                def held_write(store, record, _real=real_write, _blocked=blocked, _release=release):
                    if record.status == "queued" and not _blocked.is_set():
                        _blocked.set()
                        _release.wait(timeout=10)
                    return _real(store, record)

                with patch.object(AgentJobStore, "_write_sync", held_write):
                    task = asyncio.create_task(call())
                    await asyncio.to_thread(blocked.wait, 5)
                    task.cancel()
                    await asyncio.sleep(0.05)
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

                self._assert_withdrawn(await _the_job_it_made(queue, name), audit)

    async def test_a_job_already_running_is_stopped_when_its_admission_cannot_be_written(self) -> None:
        orchestrator = _SlowOrchestrator()
        audit = _Audit()
        audit.hold = audit.fail_after_hold = True
        queue = self._new_queue("running", audit=audit, orchestrator=orchestrator)
        await self._started(queue)
        task = asyncio.create_task(queue.enqueue_run("session-15", REQUEST))
        await asyncio.wait_for(orchestrator.started.wait(), timeout=5)

        audit.release.set()
        with self.assertRaises(OSError):
            await task

        [job] = [record.model_dump() for record in await queue.store.list()]
        self.assertEqual((job["status"], job["error"]), ("interrupted", "agent_job_interrupted"))
        self.assertTrue(orchestrator.cancelled.is_set(), "the run was left going")

    async def test_a_job_that_ran_to_its_end_before_the_audit_failed_answers_as_run(self) -> None:
        # A caller told of a failure would run it again.
        for outcome in ("completed", "failed"):
            with self.subTest(outcome=outcome):
                audit = _Audit()
                audit.hold = audit.fail_after_hold = True
                orchestrator = FakeOrchestrator()
                if outcome == "failed":
                    orchestrator.run = AsyncMock(side_effect=RuntimeError("the provider crashed"))  # type: ignore[method-assign]
                queue = self._new_queue(f"ended-{outcome}", audit=audit, orchestrator=orchestrator)
                await self._started(queue)
                task = asyncio.create_task(queue.enqueue_run("session-15", REQUEST))
                await asyncio.wait_for(audit.reached.wait(), timeout=5)
                [record] = await queue.store.list()
                self.assertEqual((await self._until_final(record.id, queue))["status"], outcome)

                audit.release.set()
                with self.assertLogs("app.agent_jobs", level="ERROR") as logged:
                    answered = await asyncio.wait_for(task, timeout=5)

                self.assertEqual(answered["id"], record.id)
                self.assertTrue(
                    any("has finished, but its queuing could not be audited" in line for line in logged.output)
                )
                self.assertEqual((await queue.get_job(record.id))["status"], outcome)

    async def test_a_job_cancelled_in_the_queue_during_the_audit_stays_cancelled_and_the_caller_is_told(self) -> None:
        audit = _Audit()
        audit.hold = audit.fail_after_hold = True
        queue = self._new_queue("cancelled", audit=audit)
        await queue.store.startup()
        task = asyncio.create_task(queue.enqueue_run("session-15", REQUEST))
        await asyncio.wait_for(audit.reached.wait(), timeout=5)
        [record] = await queue.store.list()
        await queue.cancel_job(record.id)

        audit.release.set()
        with self.assertRaises(OSError):
            await task

        self.assertEqual((await queue.get_job(record.id))["status"], "cancelled")

    async def test_a_cancelled_caller_is_never_answered_as_run_even_when_its_job_has(self) -> None:
        audit = _Audit()
        audit.hold = True
        queue = self._new_queue("cancelled-late", audit=audit)
        await self._started(queue)
        task = asyncio.create_task(queue.enqueue_run("session-15", REQUEST))
        await asyncio.wait_for(audit.reached.wait(), timeout=5)
        [record] = await queue.store.list()
        await self._until_final(record.id, queue)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class CallerSurfaceTests(_QueueCase):
    """The same through what a REST client and an MCP client call."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.audit.failing.add("agent_job_enqueued")
        await self.queue.store.startup()  # no worker: a job that is not withdrawn stays queued
        manager = BrowserManager(_settings(self.root))
        manager.get_session = AsyncMock(return_value=SimpleNamespace(id="s1"))  # type: ignore[method-assign]
        app = FastAPI()
        app.include_router(create_agent_router(manager=manager, orchestrator=SimpleNamespace(), job_queue=self.queue))
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.http.aclose)
        self.gateway = McpToolGateway(
            manager=manager,
            orchestrator=SimpleNamespace(),
            job_queue=self.queue,
            tool_profile="full",
            tool_packs="agent_jobs",
        )

    async def _the_withdrawn_jobs(self) -> list[tuple[str, str | None]]:
        return [(record.status, record.error) for record in await self.queue.store.list()]

    async def test_the_rest_routes_withdraw_the_job_and_answer_500(self) -> None:
        for path in ("/sessions/s1/agent/jobs/run", "/sessions/s1/agent/jobs/step"):
            with self.subTest(path=path), self.assertLogs("app", level="ERROR"):
                response = await self.http.post(path, json={"provider": "openai", "goal": "g"})

                self.assertEqual((response.status_code, response.json()), (500, {"detail": "Internal error"}))

        self.assertEqual(await self._the_withdrawn_jobs(), [("interrupted", "agent_job_interrupted")] * 2)
        self.assertEqual(self.orchestrator.run_calls, [])

    async def test_the_rest_resume_route_withdraws_the_resumed_job_and_answers_500(self) -> None:
        source = await _an_interrupted_run(self.queue)

        with self.assertLogs("app", level="ERROR"):
            response = await self.http.post(f"/agent/jobs/{source}/resume")

        self.assertEqual((response.status_code, response.json()), (500, {"detail": "Internal error"}))
        [resumed] = [record for record in await self.queue.store.list() if record.parent_job_id == source]
        self.assertEqual((resumed.status, resumed.error), ("interrupted", "agent_job_interrupted"))

    async def test_the_mcp_queue_tools_withdraw_the_job_and_answer_an_error(self) -> None:
        for name, request in (
            ("browser.queue_agent_run", {"provider": "openai", "goal": "g"}),
            ("browser.queue_agent_step", {"provider": "openai", "goal": "g"}),
        ):
            with self.subTest(tool=name):
                answer = await self.gateway.call_tool(
                    McpToolCallRequest(name=name, arguments={"session_id": "s1", "request": request})
                )

                self.assertTrue(answer.isError, answer.content[0].text)

        self.assertEqual(await self._the_withdrawn_jobs(), [("interrupted", "agent_job_interrupted")] * 2)

    async def test_the_mcp_resume_tool_withdraws_the_resumed_job_and_answers_an_error(self) -> None:
        source = await _an_interrupted_run(self.queue)

        answer = await self.gateway.call_tool(
            McpToolCallRequest(name="browser.resume_agent_job", arguments={"job_id": source})
        )

        self.assertTrue(answer.isError, answer.content[0].text)
        [resumed] = [record for record in await self.queue.store.list() if record.parent_job_id == source]
        self.assertEqual((resumed.status, resumed.error), ("interrupted", "agent_job_interrupted"))


if __name__ == "__main__":
    unittest.main()
