"""A governed tool call whose arguments hold a lone surrogate can be asked for, approved and spent.

A governed call is bound to its approval by a digest of its whole arguments, taken over their UTF-8, and UTF-8 cannot
encode a lone surrogate (a JSON "\\ud800" escape decodes to one). harness.start_convergence takes two values its
input does not type, the contract's metadata and a mock observation: with a lone surrogate in either, asking for the
approval answered "utf-8", the first argument of the error the digest raised. What the harness then does with the
value is in tests/test_lone_surrogate_records.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.harness.iterate import HarnessService
from app.models import McpToolCallRequest
from app.tool_gateway.gateway import McpToolGateway
from tests.test_approval_spend import SpendBase

# A lone surrogate, as a JSON "\ud800" escape decodes to: UTF-8 cannot encode it.
LONE = "\ud800"
ANOTHER = "\udfff"
CONTRACT = {
    "id": "example-read",
    "goal": "Open the example page and confirm done state.",
    "postconditions": [{"kind": "url_contains", "value": "example.com/done"}],
    "budget": {"max_attempts": 1, "max_steps": 2, "max_wall_seconds": 300},
}


def _arguments(lone: str) -> dict[str, Any]:
    return {
        "contract": {**CONTRACT, "metadata": {"note": f"n{lone}"}},
        "mock_final_observation": {"url": "https://example.com/done", "text": f"done{lone}"},
    }


class AGovernedCallsArgumentsTests(SpendBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=SimpleNamespace(),
            job_queue=SimpleNamespace(),
            tool_profile="full",
            harness_service=HarnessService(Path(self.tempdir.name) / "harness"),
        )

    async def start(self, **arguments: Any):
        request = McpToolCallRequest(
            name="harness.start_convergence", arguments={"workflow_profile": "governed", **arguments}
        )
        return await self.gateway.call_tool(request)

    async def test_a_governed_call_whose_arguments_hold_a_lone_surrogate_is_asked_and_then_spent(self) -> None:
        asked = await self.start(**_arguments(LONE))

        self.assertEqual(asked.structuredContent.get("status"), "approval_required", asked.content[0].text)
        approval_id = asked.structuredContent["approval_id"]
        # What the operator is shown names the character by its escape, and is what was sent.
        shown = json.loads(await self.manager.approvals.arguments(approval_id))
        self.assertEqual(shown["contract"]["metadata"], {"note": f"n{LONE}"})
        self.assertIn("\\ud800", (await self.manager.approvals.get(approval_id)).reason)

        await self.manager.approve(approval_id, comment="ok")
        other = await self.start(**_arguments(ANOTHER), approval_id=approval_id)
        ran = await self.start(**_arguments(LONE), approval_id=approval_id)

        # The approval is of this call: another lone surrogate in the same places is another call.
        self.assertTrue(other.isError, other.content[0].text)
        self.assertFalse(ran.isError, ran.content[0].text)
        self.assertEqual(ran.structuredContent["status"], "converged", ran.content[0].text)
        self.assertEqual(await self.status(approval_id), "executed")
