"""A disabled cron job must reach an MCP agent as a clear refusal it can act on."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.cron_service import CronJobDisabledError
from app.models import McpToolCallRequest
from app.tool_gateway import McpToolGateway


def _gateway(error: Exception) -> McpToolGateway:
    return McpToolGateway(
        manager=SimpleNamespace(settings=SimpleNamespace(force_governed_approvals=False, autonomous_approvals=False)),
        orchestrator=SimpleNamespace(),
        job_queue=SimpleNamespace(),
        tool_profile="full",
        cron_service=SimpleNamespace(trigger_job=AsyncMock(side_effect=error)),
        share_manager=Mock(),
        proxy_store=Mock(),
        vision_targeter=Mock(),
    )


async def test_triggering_a_disabled_job_says_the_job_is_disabled() -> None:
    gateway = _gateway(CronJobDisabledError("job is disabled; enable it to run it"))

    response = await gateway.call_tool(McpToolCallRequest(name="browser.trigger_cron_job", arguments={"job_id": "j1"}))

    assert response.isError
    assert "disabled" in response.content[0].text
    assert response.structuredContent["code"] == "cron_job_disabled"
    assert "Tool execution failed" not in response.content[0].text
