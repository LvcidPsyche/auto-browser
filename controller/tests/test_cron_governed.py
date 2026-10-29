"""Cron tools under governed approvals.

The cron tools (create, trigger, delete) used to carry no governed kind, so neither a caller's
workflow_profile=governed nor FORCE_GOVERNED_APPROVALS gated them, and a job's run always started
with the request model's default profile, `fast`, whatever profile the creating caller ran under.

Entry point: McpToolGateway.call_tool, with a real CronService.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.browser_manager import BrowserManager
from app.cron_service import CronService
from app.models import McpToolCallRequest
from app.routes.operations import create_operations_router
from app.tool_gateway.gateway import McpToolGateway
from tests.test_browser_manager_create_session import _settings


class CronGovernedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "crons.json"
        self.settings = _settings(root)
        self.manager = BrowserManager(self.settings)
        self.manager.create_session = AsyncMock(return_value={"id": "cron-session"})  # type: ignore[method-assign]
        self.delete_memory = AsyncMock(return_value=True)
        self.manager.memory = SimpleNamespace(delete=self.delete_memory)  # type: ignore[assignment]
        self.queue = MagicMock()
        self.queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
        self.cron = CronService(self.store, job_queue=self.queue, manager=self.manager)
        self.gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=SimpleNamespace(),
            job_queue=self.queue,
            tool_profile="full",
            tool_packs="cron",
            cron_service=self.cron,
        )

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _call(self, name: str, arguments: dict):
        return await self.gateway.call_tool(McpToolCallRequest(name=name, arguments=arguments))

    async def _call_approved(self, name: str, arguments: dict):
        """The call as an operator-approved agent makes it: approve, then repeat."""
        response = await self._call(name, arguments)
        if (response.structuredContent or {}).get("status") != "approval_required":
            return response
        approval_id = response.structuredContent["approval_id"]
        await self.manager.approve(approval_id)
        return await self._call(name, {**arguments, "approval_id": approval_id})

    async def _create(self, **extra):
        return await self._call(
            "browser.create_cron_job",
            {
                "name": "j",
                "goal": "delete the account",
                "webhook_enabled": True,
                "workflow_profile": "governed",
                **extra,
            },
        )

    def _assert_approval_required(self, response) -> None:
        self.assertEqual(response.structuredContent.get("status"), "approval_required", response.structuredContent)

    async def test_control_governed_sessionless_write_needs_approval(self) -> None:
        pending = await self._call(
            "browser.delete_memory_profile", {"profile_name": "p", "workflow_profile": "governed"}
        )
        self._assert_approval_required(pending)
        self.delete_memory.assert_not_awaited()

    async def test_control_forced_sessionless_write_needs_approval(self) -> None:
        # Positive control for the forced trigger test: flipping the setting after
        # the gateway is built does reach the gate.
        self.settings.force_governed_approvals = True
        pending = await self._call("browser.delete_memory_profile", {"profile_name": "p"})
        self._assert_approval_required(pending)
        self.delete_memory.assert_not_awaited()

    async def test_governed_create_cron_job_needs_approval(self) -> None:
        self._assert_approval_required(await self._create())
        self.assertEqual(await self.cron.list_jobs(), [])

    async def test_governed_trigger_cron_job_needs_approval(self) -> None:
        job = await self.cron.create_job(name="j", goal="delete the account")
        response = await self._call("browser.trigger_cron_job", {"job_id": job["id"], "workflow_profile": "governed"})
        self._assert_approval_required(response)
        self.queue.enqueue_run.assert_not_awaited()

    async def test_governed_delete_cron_job_needs_approval(self) -> None:
        job = await self.cron.create_job(name="j", goal="x")
        response = await self._call("browser.delete_cron_job", {"job_id": job["id"], "workflow_profile": "governed"})
        self._assert_approval_required(response)
        self.assertEqual(len(await self.cron.list_jobs()), 1)

    async def test_forced_governed_trigger_cron_job_needs_approval(self) -> None:
        # FORCE_GOVERNED_APPROVALS on: the server decides.
        self.settings.force_governed_approvals = True
        job = await self.cron.create_job(name="j", goal="delete the account")
        response = await self._call("browser.trigger_cron_job", {"job_id": job["id"]})
        self._assert_approval_required(response)
        self.queue.enqueue_run.assert_not_awaited()

    async def test_governed_caller_job_runs_governed_after_any_approval_round_trip(self) -> None:
        created = await self._call_approved(
            "browser.create_cron_job",
            {"name": "j", "goal": "delete the account", "webhook_enabled": True, "workflow_profile": "governed"},
        )
        self.assertFalse(created.isError, created.structuredContent)
        triggered = await self._call_approved(
            "browser.trigger_cron_job", {"job_id": created.structuredContent["id"], "workflow_profile": "governed"}
        )
        self.assertFalse(triggered.isError, triggered.structuredContent)
        self.queue.enqueue_run.assert_awaited_once()
        request = self.queue.enqueue_run.await_args.args[1]
        self.assertEqual(request.workflow_profile, "governed")

    async def test_policy_profile_alias_is_stored_too(self) -> None:
        created = await self._call_approved(
            "browser.create_cron_job",
            {"name": "j", "goal": "g", "webhook_enabled": True, "policy_profile": "governed"},
        )
        self.assertFalse(created.isError, created.structuredContent)
        self.assertEqual(created.structuredContent["workflow_profile"], "governed")

    async def test_scheduled_run_uses_the_stored_profile(self) -> None:
        created = await self._call_approved(
            "browser.create_cron_job",
            {"name": "j", "goal": "g", "webhook_enabled": True, "workflow_profile": "governed"},
        )
        await self.cron._scheduled_run(created.structuredContent["id"])
        request = self.queue.enqueue_run.await_args.args[1]
        self.assertEqual(request.workflow_profile, "governed")

    async def test_forced_creation_does_not_bake_in_the_forced_profile(self) -> None:
        # Force decides at call time; the job keeps what its caller chose, so
        # turning the setting off later is not silently overridden by old jobs.
        self.settings.force_governed_approvals = True
        created = await self._call_approved(
            "browser.create_cron_job", {"name": "j", "goal": "g", "webhook_enabled": True}
        )
        self.assertFalse(created.isError, created.structuredContent)
        self.assertEqual(created.structuredContent["workflow_profile"], "fast")

    async def test_job_stored_by_an_older_version_still_runs_as_before(self) -> None:
        legacy = {
            "id": "legacy00001",
            "name": "old",
            "provider": "openai",
            "schedule": None,
            "enabled": True,
            "webhook_key": None,
            "goal": "g",
            "start_url": None,
            "auth_profile": None,
            "proxy_persona": None,
            "max_steps": 20,
            "owner": None,
            "created_at": "2026-01-01T00:00:00Z",
            "last_run_at": None,
            "last_status": None,
            "run_count": 0,
        }
        self.store.write_text(json.dumps({"legacy00001": legacy}), encoding="utf-8")
        await self.cron.trigger_job("legacy00001")
        request = self.queue.enqueue_run.await_args.args[1]
        self.assertEqual(request.workflow_profile, "fast")

    async def test_rest_style_job_without_a_profile_runs_fast(self) -> None:
        job = await self.cron.create_job(name="j", goal="g")
        await self.cron.trigger_job(job["id"])
        request = self.queue.enqueue_run.await_args.args[1]
        self.assertEqual(request.workflow_profile, "fast")


class StoredStartUrlTests(unittest.IsolatedAsyncioTestCase):
    """Item 32: a job an earlier version stored with a start_url whose host a browser reads otherwise."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.manager = BrowserManager(_settings(root))
        # Right after the start_url checks, before anything opens a browser: a run that gets here was let through.
        self.past_the_checks = MagicMock(side_effect=RuntimeError("the run got past the start_url checks"))
        self.manager._check_session_limit = self.past_the_checks  # type: ignore[method-assign]
        self.queue = MagicMock()
        self.queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
        self.cron = CronService(root / "crons.json", job_queue=self.queue, manager=self.manager)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_its_run_fails_saying_why_and_the_scheduler_keeps_going(self) -> None:
        # Slashless: the allowlist reads example.com as a browser does, urllib reads no host.
        job = await self.cron.create_job(name="j", goal="g", start_url="https:example.com/")

        for fire in range(2):  # the second fire is not skipped as a run still in flight
            with self.subTest(fire=fire), self.assertLogs("app.cron_service", "ERROR") as logged:
                await self.cron._scheduled_run(job["id"])
            text = " | ".join(logged.output)
            self.assertIn(job["id"], text)
            self.assertIn("start_url", text)
            self.assertIn("another host", text)

        self.past_the_checks.assert_not_called()
        self.queue.enqueue_run.assert_not_called()
        self.assertEqual((self.cron._active_runs, self.manager.sessions), ({}, {}))


class QueuedAgentJobGovernedTests(unittest.IsolatedAsyncioTestCase):
    """SG-1: a governed caller queued an agent run or step whose own request said `fast`, and the queued job's
    writes then skipped approval: the cron gap, through browser.queue_agent_run and browser.queue_agent_step."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = BrowserManager(_settings(Path(self.tmp.name)))
        self.manager.get_session = AsyncMock(return_value=SimpleNamespace(id="s1"))  # type: ignore[method-assign]
        self.queue = MagicMock()
        self.queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
        self.queue.enqueue_step = AsyncMock(return_value={"id": "agent-job-2"})
        self.gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=SimpleNamespace(),
            job_queue=self.queue,
            tool_profile="full",
            tool_packs="agent_jobs",
        )

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _queued_profile(self, tool: str, enqueue: AsyncMock, caller: str, requested: str) -> str:
        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name=tool,
                arguments={
                    "session_id": "s1",
                    "workflow_profile": caller,
                    "request": {"provider": "openai", "goal": "delete the account", "workflow_profile": requested},
                },
            )
        )
        self.assertFalse(response.isError, response.content)
        return enqueue.await_args.args[1].workflow_profile

    async def test_a_run_a_governed_caller_queues_runs_governed(self) -> None:
        profile = await self._queued_profile("browser.queue_agent_run", self.queue.enqueue_run, "governed", "fast")
        self.assertEqual(profile, "governed")

    async def test_a_step_a_governed_caller_queues_runs_governed(self) -> None:
        profile = await self._queued_profile("browser.queue_agent_step", self.queue.enqueue_step, "governed", "fast")
        self.assertEqual(profile, "governed")

    async def test_control_a_run_a_fast_caller_queues_keeps_its_own_profile(self) -> None:
        for requested in ("fast", "governed"):
            profile = await self._queued_profile("browser.queue_agent_run", self.queue.enqueue_run, "fast", requested)
            self.assertEqual(profile, requested)


class RestCronProfileTests(unittest.IsolatedAsyncioTestCase):
    """SG-4: POST /crons accepted workflow_profile in its body and silently stored `fast`. REST callers are
    operators, so the profile the body names is stored, `fast` by default."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.manager = BrowserManager(_settings(root))
        self.manager.create_session = AsyncMock(return_value={"id": "cron-session"})  # type: ignore[method-assign]
        self.queue = MagicMock()
        self.queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
        self.cron = CronService(root / "crons.json", job_queue=self.queue, manager=self.manager)
        app = FastAPI()
        app.include_router(create_operations_router(manager=self.manager, proxy_store=None, cron_service=self.cron))
        self.client = TestClient(app, raise_server_exceptions=False)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def _create(self, **extra) -> dict:
        response = self.client.post(
            "/crons", json={"name": "j", "goal": "delete the account", "webhook_enabled": True, **extra}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_a_governed_job_created_over_rest_is_stored_and_runs_governed(self) -> None:
        job = self._create(workflow_profile="governed")
        self.assertEqual(job["workflow_profile"], "governed")
        await self.cron.trigger_job(job["id"])
        self.assertEqual(self.queue.enqueue_run.await_args.args[1].workflow_profile, "governed")

    async def test_control_a_job_created_over_rest_without_a_profile_stays_fast(self) -> None:
        job = self._create()
        self.assertEqual(job["workflow_profile"], "fast")
        await self.cron.trigger_job(job["id"])
        self.assertEqual(self.queue.enqueue_run.await_args.args[1].workflow_profile, "fast")


if __name__ == "__main__":
    unittest.main()
