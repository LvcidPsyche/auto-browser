"""One approval, one execution — on every path that executes an approved action.

#160 made execution claim the approval so a retried request could not run it
twice. But the claim was taken at each layer that saw the approval: the MCP
gateway claimed a governed approval and then `execute_decision` claimed the
same approval again, so every governed payment/post/account_change/destructive
action and every governed upload failed with "already being executed" and never
ran. The opposite gap sat beside it: a governed `write` approval (the kind the
runtime itself does not require) was never consumed on the orchestrator path or
by POST /approvals/{id}/execute, so one approval ran its action any number of
times. These tests drive the real gateway, manager and store.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.approvals import ApprovalRequiredError
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import BrowserActionDecision, McpToolCallRequest
from app.orchestrator import BrowserOrchestrator
from app.providers.base import ProviderDecision
from app.tool_gateway.gateway import McpToolGateway
from app.utils import UTC

SESSION = "session-1"


class FakePage:
    url = "https://example.com"

    async def title(self) -> str:
        return "Example"


class ApprovalExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        settings = Settings(_env_file=None)
        for attr in ("artifact_root", "upload_root", "auth_root", "approval_root", "session_store_root", "audit_root"):
            setattr(settings, attr, str(root / attr))
        self.manager = BrowserManager(settings)
        artifact_dir = Path(settings.artifact_root) / SESSION
        artifact_dir.mkdir(parents=True)
        session = BrowserSession(
            id=SESSION,
            name=SESSION,
            created_at=datetime.now(UTC),
            context=object(),  # type: ignore[arg-type]
            page=FakePage(),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(settings.auth_root) / SESSION,
            upload_dir=Path(settings.upload_root) / SESSION,
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        session.auth_dir.mkdir(parents=True)
        session.upload_dir.mkdir(parents=True)
        self.manager.sessions[SESSION] = session
        self.manager.click = AsyncMock(return_value={"action": "click"})  # type: ignore[method-assign]
        self.gateway = McpToolGateway(manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def _governed_call(self, action: dict, approval_id: str | None = None):
        arguments = {"session_id": SESSION, "workflow_profile": "governed", "action": action}
        if approval_id:
            arguments["approval_id"] = approval_id
        return await self.gateway.call_tool(McpToolCallRequest(name="browser.execute_action", arguments=arguments))

    async def _approved_governed_call(self, action: dict) -> str:
        pending = await self._governed_call(action)
        self.assertEqual(pending.structuredContent.get("status"), "approval_required", pending.structuredContent)
        approval_id = pending.structuredContent["approval_id"]
        await self.manager.approve(approval_id, comment="ok")
        return approval_id

    async def _status(self, approval_id: str) -> str:
        return (await self.manager.approvals.get(approval_id)).status

    async def test_governed_high_risk_action_through_the_gateway_runs_once(self) -> None:
        action = {"action": "click", "reason": "pay", "element_id": "op-pay", "risk_category": "payment"}
        approval_id = await self._approved_governed_call(action)

        response = await self._governed_call(action, approval_id)

        self.assertFalse(response.isError, response.content[0].text)
        self.manager.click.assert_awaited_once()
        self.assertEqual(await self._status(approval_id), "executed")
        replay = await self._governed_call(action, approval_id)
        self.assertTrue(replay.isError)
        self.manager.click.assert_awaited_once()

    async def test_governed_upload_through_the_gateway_runs_once(self) -> None:
        (Path(self.manager.settings.upload_root) / "demo.txt").write_text("x", encoding="utf-8")
        self.manager._run_action = AsyncMock(return_value={"action": "upload"})  # type: ignore[method-assign]
        action = {"action": "upload", "reason": "attach", "selector": "input[type=file]", "file_path": "demo.txt"}
        approval_id = await self._approved_governed_call(action)

        response = await self._governed_call(action, approval_id)

        self.assertFalse(response.isError, response.content[0].text)
        self.manager._run_action.assert_awaited_once()
        self.assertEqual(await self._status(approval_id), "executed")

    async def test_concurrent_governed_calls_run_the_action_once(self) -> None:
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_click(*_args, **_kwargs):
            started.set()
            await release.wait()
            return {"action": "click"}

        self.manager.click = AsyncMock(side_effect=slow_click)  # type: ignore[method-assign]
        action = {"action": "click", "reason": "post it", "element_id": "op-post", "risk_category": "post"}
        approval_id = await self._approved_governed_call(action)

        first = asyncio.create_task(self._governed_call(action, approval_id))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            second = await asyncio.wait_for(self._governed_call(action, approval_id), timeout=5)
        finally:
            release.set()
        first_response = await first

        self.assertFalse(first_response.isError, first_response.content[0].text)
        self.assertTrue(second.isError)
        self.manager.click.assert_awaited_once()

    async def test_orchestrator_governed_write_consumes_its_approval(self) -> None:
        decision = BrowserActionDecision(action="click", reason="save", element_id="op-save", risk_category="write")
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.require_governed_approval(SESSION, decision, approval_id=None)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id, comment="ok")
        orchestrator = BrowserOrchestrator(self.manager, SimpleNamespace())
        provider_decision = ProviderDecision(provider="openai", model="m", decision=decision, usage=None, raw_text=None)

        async def step():
            return await orchestrator._execute_decision(
                session_id=SESSION,
                goal="save",
                observation={},
                provider_decision=provider_decision,
                upload_approved=False,
                approval_id=approval_id,
                workflow_profile="governed",
            )

        await step()
        self.assertEqual(await self._status(approval_id), "executed")
        with self.assertRaises(PermissionError):
            await step()
        self.manager.click.assert_awaited_once()

    async def test_execute_approval_consumes_a_governed_write_approval(self) -> None:
        decision = BrowserActionDecision(action="click", reason="save", element_id="op-save", risk_category="write")
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.require_governed_approval(SESSION, decision, approval_id=None)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id, comment="ok")

        result = await self.manager.execute_approval(approval_id)

        self.assertEqual(result["approval"]["status"], "executed")
        with self.assertRaises(PermissionError):
            await self.manager.execute_approval(approval_id)
        self.manager.click.assert_awaited_once()

    async def test_concurrent_execute_approval_requests_run_the_action_once(self) -> None:
        """GHSA-q7xx-f6pw-w7mv's reproduction: N parallel POST /approvals/{id}/execute."""
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_click(*_args, **_kwargs):
            started.set()
            await release.wait()
            return {"action": "click"}

        self.manager.click = AsyncMock(side_effect=slow_click)  # type: ignore[method-assign]
        decision = BrowserActionDecision(action="click", reason="post it", element_id="op-post", risk_category="post")
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.execute_decision(SESSION, decision)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id, comment="ok")

        first = asyncio.create_task(self.manager.execute_approval(approval_id))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            others = await asyncio.gather(
                *(self.manager.execute_approval(approval_id) for _ in range(7)), return_exceptions=True
            )
        finally:
            release.set()
        await first

        self.assertTrue(all(isinstance(result, PermissionError) for result in others), others)
        self.manager.click.assert_awaited_once()
        self.assertEqual(await self._status(approval_id), "executed")

    async def test_an_expired_approval_is_refused_before_its_action_runs(self) -> None:
        decision = BrowserActionDecision(action="click", reason="save", element_id="op-save", risk_category="write")
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.require_governed_approval(SESSION, decision, approval_id=None)
        approval = await self.manager.approvals.approve(ctx.exception.approval.id, comment="ok")
        approval.approved_expires_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        await self.manager.approvals.file_store.upsert(approval)

        with self.assertRaises(PermissionError):
            await self.manager.execute_approval(approval.id)
        self.manager.click.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
