"""A job a token-verified operator queued does not run once that operator's named credential is gone."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.agent_jobs import QUEUED_OPERATOR_CREDENTIAL_REMOVED, AgentJobQueue
from app.app_factory import build_controller_services
from app.config import Settings
from app.models import AgentRunRequest, OperatorIdentity
from app.session_ownership import acting_as
from tests.test_agent_jobs import FakeOrchestrator, every_session_usable

ALICE = OperatorIdentity(id="alice", source="token")
ALICE_BY_HEADER = OperatorIdentity(id="alice", source="header")
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "discarded"}


class QueuedOperatorCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.configured = {"alice"}
        self.audit = MagicMock()
        self.audit.append = AsyncMock()
        self.orchestrator = FakeOrchestrator()
        self.on_finish = AsyncMock()
        self.queue = AgentJobQueue(
            orchestrator=self.orchestrator,
            store_root=Path(self.tmp.name) / "jobs",
            session_accessible=every_session_usable,
            worker_count=1,
            audit_store=self.audit,
            operator_configured=lambda operator_id: operator_id in self.configured,
        )
        await self.queue.startup()
        self.addAsyncCleanup(self.queue.shutdown)

    async def _run(self, operator: OperatorIdentity | None):
        with acting_as(operator):
            queued = await self.queue.enqueue_run(
                "session-1", AgentRunRequest(provider="openai", goal="g", max_steps=1), on_finish=self.on_finish
            )
        for _ in range(250):
            record = await self.queue.store.get(queued["id"])
            if record.status in TERMINAL and not self.queue._running_tasks:
                return record
            await asyncio.sleep(0.02)
        self.fail(f"the job never finished: {record.status}")

    def _events(self) -> list[str]:
        return [call.kwargs["event_type"] for call in self.audit.append.await_args_list]

    async def test_a_job_whose_token_verified_operator_was_removed_fails_and_never_runs(self) -> None:
        self.configured.clear()

        record = await self._run(ALICE)

        self.assertEqual(record.status, "failed")
        self.assertEqual(record.error, QUEUED_OPERATOR_CREDENTIAL_REMOVED)
        self.assertEqual(self.orchestrator.run_calls, [], "a refused job ran")
        self.assertNotIn("agent_job_started", self._events())
        self.assertIn("agent_job_finished", self._events())
        self.on_finish.assert_awaited_once_with(record.id)

    async def test_a_still_configured_operator_runs(self) -> None:
        record = await self._run(ALICE)

        self.assertEqual(record.status, "completed")
        self.assertEqual(len(self.orchestrator.run_calls), 1)

    async def test_an_operator_named_only_by_a_header_or_by_nobody_is_not_affected(self) -> None:
        self.configured.clear()
        for name, operator in (("header", ALICE_BY_HEADER), ("nobody", None)):
            with self.subTest(name):
                self.on_finish.reset_mock()
                record = await self._run(operator)

                self.assertEqual(record.status, "completed", record.error)

    async def test_a_job_a_cancel_reached_first_stays_cancelled(self) -> None:
        # Only a queued job can be refused: one already taken back is left as it is.
        self.configured.clear()
        queued = await self.queue.store.create(
            session_id="session-1",
            kind="agent_run",
            request=AgentRunRequest(provider="openai", goal="g", max_steps=1).model_dump(),
            operator=ALICE,
        )
        await self.queue.store.request_cancel(queued.id)

        await self.queue._process_job(queued.id)

        self.assertEqual((await self.queue.store.get(queued.id)).status, "cancelled")
        self.assertEqual(self.orchestrator.run_calls, [])


class QueuedOperatorCredentialWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_controller_wires_the_check_to_its_settings(self) -> None:
        # A job store of its own: this starts a real queue, and one on the suite's shared root would first queue
        # again, and run, whatever other tests left there, inside the bound below.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = Settings(API_BEARER_TOKENS="alice:token-a", JOB_STORE_ROOT=str(Path(tmp.name) / "jobs"))
        services = build_controller_services(settings, version="test")
        queue = services.job_queue
        self.assertEqual(queue.store.root, Path(tmp.name) / "jobs")
        queue.orchestrator = FakeOrchestrator()
        queue.session_accessible = every_session_usable
        await queue.startup()
        self.addAsyncCleanup(queue.shutdown)
        self.assertEqual(await queue.store.list(), [])

        async def run_as(operator: OperatorIdentity) -> str:
            with acting_as(operator):
                queued = await queue.enqueue_run("session-1", AgentRunRequest(provider="openai", goal="g", max_steps=1))
            for _ in range(250):
                status = (await queue.store.get(queued["id"])).status
                if status in TERMINAL:
                    return status
                await asyncio.sleep(0.02)
            self.fail("the job never finished")

        self.assertEqual(await run_as(ALICE), "completed")
        services.settings.api_bearer_tokens = "bob:token-b"
        self.assertEqual(await run_as(ALICE), "failed")
