"""MCP_FORCE_GOVERNED: the server, not the caller, decides that writes need approval.

`workflow_profile` is an argument the calling agent supplies and defaults to
`fast`, so on a multi-operator deployment governed approvals were whatever each
agent chose. With MCP_FORCE_GOVERNED=true every MCP tool that has a
`governed_kind` runs as governed, whatever the caller passed. It is off by
default: turning it on stalls every agent loop on its first write until an
operator approves.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.approvals import CONTROLLER_SCOPE
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import McpToolCallRequest
from app.tool_gateway.gateway import McpToolGateway
from app.utils import UTC
from tests.test_browser_manager_create_session import _settings

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
        self.assertFalse(Settings(_env_file=None).mcp_force_governed)
        self.assertTrue(Settings(_env_file=None, MCP_FORCE_GOVERNED=True).mcp_force_governed)

    async def test_off_a_fast_call_runs_without_approval(self) -> None:
        for call in (self._session_write, self._sessionless_write):
            with self.subTest(call=call.__name__):
                response = await call()
                self.assertFalse(response.isError, response.content[0].text)
        self.manager.click.assert_awaited_once()
        self.delete.assert_awaited_once()

    async def test_forced_a_fast_session_call_needs_approval_then_runs(self) -> None:
        self.settings.mcp_force_governed = True

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
        self.settings.mcp_force_governed = True

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
        self.settings.mcp_force_governed = True

        for spelling in ({"workflow_profile": "fast"}, {"policy_profile": "fast"}, {"workflow_profile": "bogus"}):
            with self.subTest(spelling=spelling):
                for call in (self._session_write, self._sessionless_write):
                    response = await call(**spelling)
                    self.assertEqual(response.structuredContent.get("status"), "approval_required")
        self.manager.click.assert_not_awaited()
        self.delete.assert_not_awaited()

    async def test_forced_a_tool_without_a_governed_kind_is_unaffected(self) -> None:
        self.settings.mcp_force_governed = True
        self.assertIsNone(self.gateway._registry.get("browser.list_sessions").governed_kind)

        response = await self._call("browser.list_sessions", {})

        self.assertFalse(response.isError, response.content[0].text)
        self.assertEqual(await self.manager.approvals.list(), [])

    async def test_forced_with_autonomous_approvals_the_agent_can_approve_its_own_call(self) -> None:
        # Documented in docs/production-hardening.md: AUTONOMOUS_APPROVALS=true
        # hands the decision back to the agent, so it cancels the forcing.
        self.settings.mcp_force_governed = True
        self.settings.autonomous_approvals = True
        pending = await self._sessionless_write()
        approval_id = pending.structuredContent["approval_id"]

        approved = await self._call("browser.approve_approval", {"approval_id": approval_id})
        ran = await self._sessionless_write(approval_id=approval_id)

        self.assertFalse(approved.isError, approved.content[0].text)
        self.assertFalse(ran.isError, ran.content[0].text)
        self.delete.assert_awaited_once()
        self.assertEqual((await self.manager.approvals.get(approval_id)).decided_via, "agent")


if __name__ == "__main__":
    unittest.main()
