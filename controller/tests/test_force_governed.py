"""FORCE_GOVERNED_APPROVALS: the server, not the caller, decides that writes need approval.

`workflow_profile` is an argument the calling agent supplies and defaults to
`fast`, so on a multi-operator deployment governed approvals were whatever each
agent chose. With FORCE_GOVERNED_APPROVALS=true every MCP tool that has a
`governed_kind` runs as governed, whatever the caller passed. It is off by
default: turning it on stalls every agent loop on its first write until an
operator approves.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.agent_jobs import AgentJobQueue
from app.approvals import CONTROLLER_SCOPE
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import AgentStepRequest, BrowserActionDecision, McpToolCallRequest
from app.orchestrator import BrowserOrchestrator
from app.providers.base import ProviderDecision
from app.tool_gateway.gateway import McpToolGateway
from app.utils import UTC
from tests.test_browser_manager_create_session import _settings
from tests.test_orchestrator import StaticRegistry

SESSION = "session-1"
CLICK = {"action": "click", "reason": "save", "element_id": "op-save", "risk_category": "write"}


class FakePage:
    url = "https://example.com"

    async def title(self) -> str:
        return "Example"


class ForceGovernedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))
        self.manager = BrowserManager(self.settings)
        artifact_dir = Path(self.settings.artifact_root) / SESSION
        artifact_dir.mkdir(parents=True)
        self.manager.sessions[SESSION] = BrowserSession(
            id=SESSION,
            name=SESSION,
            created_at=datetime.now(UTC),
            context=object(),  # type: ignore[arg-type]
            page=FakePage(),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=artifact_dir / "auth",
            upload_dir=artifact_dir / "uploads",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        self.manager.click = AsyncMock(return_value={"action": "click"})  # type: ignore[method-assign]
        self.delete = AsyncMock(return_value=True)
        self.manager.memory = SimpleNamespace(delete=self.delete)  # type: ignore[assignment]
        self.gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=SimpleNamespace(),
            job_queue=SimpleNamespace(),
            tool_profile="full",
        )

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _call(self, name: str, arguments: dict):
        return await self.gateway.call_tool(McpToolCallRequest(name=name, arguments=arguments))

    async def _session_write(self, **extra):
        return await self._call("browser.execute_action", {"session_id": SESSION, "action": CLICK, **extra})

    async def _sessionless_write(self, **extra):
        return await self._call("browser.delete_memory_profile", {"profile_name": "shared-login", **extra})

    def test_the_setting_is_off_by_default_and_reads_its_env_name(self) -> None:
        self.assertFalse(Settings(_env_file=None).force_governed_approvals)
        self.assertTrue(Settings(_env_file=None, FORCE_GOVERNED_APPROVALS=True).force_governed_approvals)

    async def test_off_a_fast_call_runs_without_approval(self) -> None:
        for call in (self._session_write, self._sessionless_write):
            with self.subTest(call=call.__name__):
                response = await call()
                self.assertFalse(response.isError, response.content[0].text)
        self.manager.click.assert_awaited_once()
        self.delete.assert_awaited_once()

    async def test_forced_a_fast_session_call_needs_approval_then_runs(self) -> None:
        self.settings.force_governed_approvals = True

        pending = await self._session_write()

        self.assertEqual(pending.structuredContent.get("status"), "approval_required", pending.structuredContent)
        self.assertEqual(pending.structuredContent["approval"]["session_id"], SESSION)
        self.manager.click.assert_not_awaited()
        approval_id = pending.structuredContent["approval_id"]
        await self.manager.approve(approval_id)
        ran = await self._session_write(approval_id=approval_id)
        self.assertFalse(ran.isError, ran.content[0].text)
        self.manager.click.assert_awaited_once()

    async def test_forced_a_fast_sessionless_call_needs_approval_then_runs(self) -> None:
        self.settings.force_governed_approvals = True

        pending = await self._sessionless_write()

        self.assertEqual(pending.structuredContent.get("status"), "approval_required", pending.structuredContent)
        self.assertEqual(pending.structuredContent["approval"]["session_id"], CONTROLLER_SCOPE)
        self.delete.assert_not_awaited()
        approval_id = pending.structuredContent["approval_id"]
        await self.manager.approve(approval_id)
        ran = await self._sessionless_write(approval_id=approval_id)
        self.assertFalse(ran.isError, ran.content[0].text)
        self.delete.assert_awaited_once()

    async def test_forced_a_caller_cannot_ask_for_fast(self) -> None:
        self.settings.force_governed_approvals = True

        for spelling in ({"workflow_profile": "fast"}, {"policy_profile": "fast"}, {"workflow_profile": "bogus"}):
            with self.subTest(spelling=spelling):
                for call in (self._session_write, self._sessionless_write):
                    response = await call(**spelling)
                    self.assertEqual(response.structuredContent.get("status"), "approval_required")
        self.manager.click.assert_not_awaited()
        self.delete.assert_not_awaited()

    async def test_forced_a_tool_without_a_governed_kind_is_unaffected(self) -> None:
        self.settings.force_governed_approvals = True
        self.assertIsNone(self.gateway._registry.get("browser.list_sessions").governed_kind)

        response = await self._call("browser.list_sessions", {})

        self.assertFalse(response.isError, response.content[0].text)
        self.assertEqual(await self.manager.approvals.list(), [])

    async def test_forced_with_autonomous_approvals_the_agent_can_approve_its_own_call(self) -> None:
        # Documented in docs/production-hardening.md: AUTONOMOUS_APPROVALS=true
        # hands the decision back to the agent, so it cancels the forcing.
        self.settings.force_governed_approvals = True
        self.settings.autonomous_approvals = True
        pending = await self._sessionless_write()
        approval_id = pending.structuredContent["approval_id"]

        approved = await self._call("browser.approve_approval", {"approval_id": approval_id})
        ran = await self._sessionless_write(approval_id=approval_id)

        self.assertFalse(approved.isError, approved.content[0].text)
        self.assertFalse(ran.isError, ran.content[0].text)
        self.delete.assert_awaited_once()
        self.assertEqual((await self.manager.approvals.get(approval_id)).decided_via, "agent")


class WritingAdapter:
    """A model that always decides to click a button that writes."""

    default_model = "test-model"

    def __init__(self) -> None:
        self.context_hints: list[str | None] = []

    async def decide(self, **kwargs):
        self.context_hints.append(kwargs.get("context_hints"))
        return ProviderDecision(
            provider="openai",
            model="test-model",
            decision=BrowserActionDecision(
                action="click", reason="Save the form", element_id="op-save", risk_category="write"
            ),
            usage=None,
            raw_text="{}",
        )


class ForceGovernedAgentLoopTests(unittest.IsolatedAsyncioTestCase):
    """The built-in agent loop takes workflow_profile from its request, default fast."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))
        self.manager = BrowserManager(self.settings)
        artifact_dir = Path(self.settings.artifact_root) / SESSION
        artifact_dir.mkdir(parents=True)
        self.manager.sessions[SESSION] = BrowserSession(
            id=SESSION,
            name=SESSION,
            created_at=datetime.now(UTC),
            context=object(),  # type: ignore[arg-type]
            page=FakePage(),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=artifact_dir / "auth",
            upload_dir=artifact_dir / "uploads",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        self.manager.click = AsyncMock(return_value={"action": "click"})  # type: ignore[method-assign]
        self.manager.observe = AsyncMock(return_value={"url": "https://example.com"})  # type: ignore[method-assign]
        self.adapter = WritingAdapter()
        self.orchestrator = BrowserOrchestrator(self.manager, StaticRegistry(self.adapter))

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_forced_a_fast_step_asks_for_approval_and_the_write_waits(self) -> None:
        self.settings.force_governed_approvals = True

        step = await self.orchestrator.step(
            session_id=SESSION, provider_name="openai", goal="save", workflow_profile="fast"
        )

        self.assertEqual((step.status, step.workflow_profile), ("approval_required", "governed"))
        self.manager.click.assert_not_awaited()
        self.assertIn("Workflow profile: governed", self.adapter.context_hints[-1] or "")

        approval_id = step.execution["approval_id"]
        await self.manager.approve(approval_id)
        approved = await self.orchestrator.step(
            session_id=SESSION, provider_name="openai", goal="save", workflow_profile="fast", approval_id=approval_id
        )
        self.assertEqual(approved.status, "acted")
        self.manager.click.assert_awaited_once()

    async def test_forced_a_fast_run_asks_for_approval(self) -> None:
        self.settings.force_governed_approvals = True

        run = await self.orchestrator.run(
            session_id=SESSION, provider_name="openai", goal="save", max_steps=3, workflow_profile="fast"
        )

        self.assertEqual((run.status, run.workflow_profile), ("approval_required", "governed"))
        self.manager.click.assert_not_awaited()

    async def test_off_a_fast_step_is_unchanged(self) -> None:
        step = await self.orchestrator.step(
            session_id=SESSION, provider_name="openai", goal="save", workflow_profile="fast"
        )

        self.assertEqual((step.status, step.workflow_profile), ("acted", "fast"))
        self.manager.click.assert_awaited_once()
        self.assertEqual(await self.manager.approvals.list(), [])
        self.assertNotIn("governed", self.adapter.context_hints[-1] or "")

    async def test_forced_a_queued_job_that_asks_for_fast_is_governed_too(self) -> None:
        self.settings.force_governed_approvals = True
        queue = AgentJobQueue(
            orchestrator=self.orchestrator,
            store_root=Path(self.tmp.name) / "jobs",
            session_owner=self.manager.session_owner,
        )
        await queue.startup()
        try:
            job = await queue.enqueue_step(SESSION, AgentStepRequest(provider="openai", goal="save"))
            self.assertEqual(job["request"]["workflow_profile"], "fast")
            for _ in range(200):
                record = await queue.store.get(job["id"])
                if record.status in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.02)
        finally:
            await queue.shutdown()

        self.assertEqual(record.status, "completed", record.error)
        self.assertEqual(record.result["status"], "approval_required")
        self.manager.click.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
