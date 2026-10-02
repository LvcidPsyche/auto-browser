"""AUTONOMOUS_APPROVALS: agents approve their own actions only when a deployment opts in.

The MCP `browser.approve_approval` tool let the agent whose action was waiting
approve it, so in the `full` tool profile an approval stopped nothing. It now
refuses unless AUTONOMOUS_APPROVALS=true. With it on, the agent may approve
(over MCP, and the built-in agent loop approves and carries on), and every such
decision is recorded as decided_via=agent. An agent can never overturn a
decision an operator already made. These tests drive the real gateway, manager,
store and orchestrator.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.approvals import ApprovalRequiredError
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import BrowserActionDecision, McpToolCallRequest
from app.orchestrator import AUTONOMOUS_APPROVAL_COMMENT, BrowserOrchestrator
from app.providers.base import ProviderDecision
from app.tool_gateway.gateway import McpToolGateway
from app.utils import UTC

SESSION = "session-1"
PAYMENT = {"action": "click", "reason": "pay", "element_id": "op-pay", "risk_category": "payment"}


class FakePage:
    url = "https://example.com"

    async def title(self) -> str:
        return "Example"


class DecidingAdapter:
    default_model = "test-model"

    def __init__(self, decision: BrowserActionDecision) -> None:
        self.decision = decision

    async def decide(self, **_kwargs) -> ProviderDecision:
        return ProviderDecision(
            provider="openai", model="test-model", decision=self.decision, usage=None, raw_text=None
        )


class AutonomousApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        settings = Settings(_env_file=None, MCP_TOOL_PROFILE="full")
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
        self.manager.sessions[SESSION] = session
        self.manager.click = AsyncMock(return_value={"action": "click"})  # type: ignore[method-assign]
        self.manager.observe = AsyncMock(return_value={"url": "https://example.com"})  # type: ignore[method-assign]
        self.gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=SimpleNamespace(),
            job_queue=SimpleNamespace(),
            tool_profile="full",
        )

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def _call(self, name: str, arguments: dict):
        return await self.gateway.call_tool(McpToolCallRequest(name=name, arguments=arguments))

    async def _pending_payment(self) -> str:
        response = await self._call(
            "browser.execute_action", {"session_id": SESSION, "workflow_profile": "governed", "action": PAYMENT}
        )
        self.assertEqual(response.structuredContent.get("status"), "approval_required", response.structuredContent)
        return response.structuredContent["approval_id"]

    async def _agent_approves(self, approval_id: str):
        return await self._call("browser.approve_approval", {"approval_id": approval_id})

    async def _record(self, approval_id: str):
        return await self.manager.approvals.get(approval_id)

    async def _decision_events(self) -> list[dict]:
        events = await self.manager.audit.list(event_type="approval_decision")
        return [event.details for event in events]

    async def test_an_agent_cannot_approve_its_own_action_by_default(self) -> None:
        approval_id = await self._pending_payment()

        response = await self._agent_approves(approval_id)

        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["code"], "not_permitted")
        self.assertIn("AUTONOMOUS_APPROVALS=true", response.structuredContent["error"])
        self.assertEqual((await self._record(approval_id)).status, "pending")
        self.manager.click.assert_not_awaited()

    async def test_with_autonomous_approvals_the_agent_approves_and_the_action_runs(self) -> None:
        self.manager.settings.autonomous_approvals = True
        approval_id = await self._pending_payment()

        response = await self._agent_approves(approval_id)
        self.assertFalse(response.isError, response.content[0].text)
        run = await self._call(
            "browser.execute_action",
            {"session_id": SESSION, "workflow_profile": "governed", "action": PAYMENT, "approval_id": approval_id},
        )

        self.assertFalse(run.isError, run.content[0].text)
        self.manager.click.assert_awaited_once()
        record = await self._record(approval_id)
        self.assertEqual((record.status, record.decided_via), ("executed", "agent"))
        self.assertEqual((await self._decision_events())[0]["decided_via"], "agent")

    async def test_operator_decisions_are_recorded_as_the_operators(self) -> None:
        approval_id = await self._pending_payment()

        await self.manager.approve(approval_id, comment="ok")

        self.assertEqual((await self._record(approval_id)).decided_via, "operator")
        self.assertEqual((await self._decision_events())[0]["decided_via"], "operator")

    async def test_an_agent_cannot_overturn_an_operator_decision(self) -> None:
        self.manager.settings.autonomous_approvals = True
        rejected = await self._pending_payment()
        await self.manager.reject(rejected, comment="no")

        response = await self._agent_approves(rejected)

        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["code"], "not_permitted")
        self.assertEqual((await self._record(rejected)).status, "rejected")

        await self.manager.approve(rejected, comment="changed my mind")
        response = await self._call("browser.reject_approval", {"approval_id": rejected})
        self.assertTrue(response.isError)
        self.assertEqual((await self._record(rejected)).status, "approved")

    async def _agent_step(self, decision: BrowserActionDecision, workflow_profile: str):
        orchestrator = BrowserOrchestrator(self.manager, SimpleNamespace(get=lambda _name: DecidingAdapter(decision)))
        return await orchestrator.step(
            session_id=SESSION,
            provider_name="openai",
            goal="buy the thing",
            workflow_profile=workflow_profile,  # type: ignore[arg-type]
        )

    async def test_the_agent_loop_waits_for_an_operator_by_default(self) -> None:
        result = await self._agent_step(BrowserActionDecision(**PAYMENT), "governed")

        self.assertEqual(result.status, "approval_required")
        self.manager.click.assert_not_awaited()

    async def test_with_autonomous_approvals_the_agent_loop_approves_and_carries_on(self) -> None:
        self.manager.settings.autonomous_approvals = True
        # A governed write needs an approval only because of the profile; a
        # payment needs one on the fast profile too.
        cases = (
            ("governed", "write", BrowserActionDecision(action="click", reason="save", element_id="op-save")),
            ("fast", "payment", BrowserActionDecision(**PAYMENT)),
        )
        for workflow_profile, kind, decision in cases:
            with self.subTest(workflow_profile=workflow_profile):
                self.manager.click.reset_mock()

                result = await self._agent_step(decision.model_copy(update={"risk_category": kind}), workflow_profile)

                self.assertEqual(result.status, "acted", result.error)
                self.manager.click.assert_awaited_once()
                executed = await self.manager.approvals.list(session_id=SESSION, status="executed")
                [approval] = [item for item in executed if item.kind == kind]
                self.assertEqual(approval.decided_via, "agent")
                self.assertEqual(approval.decision_comment, AUTONOMOUS_APPROVAL_COMMENT)

    async def test_an_action_an_operator_rejected_waits_for_an_operator_when_asked_again(self) -> None:
        self.manager.settings.autonomous_approvals = True
        decision = BrowserActionDecision(**PAYMENT)
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.execute_decision(SESSION, decision)
        await self.manager.reject(ctx.exception.approval.id, comment="no")

        result = await self._agent_step(decision, "fast")

        self.assertEqual(result.status, "approval_required")
        self.manager.click.assert_not_awaited()
        retry_id = result.execution["approval_id"]
        self.assertNotEqual(retry_id, ctx.exception.approval.id)
        self.assertEqual((await self._record(retry_id)).status, "pending")
        response = await self._agent_approves(retry_id)
        self.assertTrue(response.isError)
        self.assertIn("only an operator", response.structuredContent["error"])


if __name__ == "__main__":
    unittest.main()
