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
from app.utils import holds_lone_surrogate_in_a_key
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

    async def test_a_lone_surrogate_in_a_key_below_the_top_level_is_refused_fast_or_governed(self) -> None:
        # The model refuses one in a top-level key of either value, naming the field. A key of a dict inside is
        # typed by nothing: the fast call ran with it, and the governed one answered "utf-8", the first argument of
        # the error its arguments raised when they were written out for the approval.
        mock = {"url": "https://example.com/done"}
        below_the_top = {
            "contract.metadata": {"contract": {**CONTRACT, "metadata": {"a": {f"k{LONE}": 1}}}},
            "contract.metadata, in a list": {"contract": {**CONTRACT, "metadata": {"a": [{f"k{LONE}": 1}]}}},
            "mock_final_observation": {"contract": CONTRACT, "mock_final_observation": {**mock, "a": {f"k{LONE}": 1}}},
        }
        for where, arguments in below_the_top.items():
            for profile in ("fast", "governed"):
                with self.subTest(where=where, profile=profile):
                    request = McpToolCallRequest(
                        name="harness.start_convergence", arguments={"workflow_profile": profile, **arguments}
                    )

                    answer = await self.gateway.call_tool(request)

                    text = answer.content[0].text
                    self.assertTrue(answer.isError, text)
                    self.assertIn("Invalid arguments for harness.start_convergence", text)
                    self.assertIn(f"{where.partition(',')[0]} must not hold a lone surrogate", text)
        self.assertEqual(await self.manager.approvals.list(), [])
        self.assertEqual(self.gateway.harness_service.list_runs(), [])

    def test_the_walk_for_such_a_key_is_stopped_by_no_depth(self) -> None:
        # It runs on a value nothing has bounded the depth of. Written by recursion it raised RecursionError from
        # the model at 500 levels, where a mock observation that deep had been taken.
        deep: Any = {f"k{LONE}": 1}
        plain: Any = {"k": f"a value is not a key{LONE}"}
        for _ in range(5000):
            deep, plain = {"a": [deep]}, {"a": [plain]}

        self.assertTrue(holds_lone_surrogate_in_a_key(deep))
        self.assertFalse(holds_lone_surrogate_in_a_key(plain))
        self.assertFalse(holds_lone_surrogate_in_a_key(f"no dict at all{LONE}"))

    def test_a_call_that_holds_no_lone_surrogate_is_bound_to_its_approval_by_the_digest_it_had(self) -> None:
        # An approval stored before an upgrade is matched by this text: a change to how the arguments are written
        # out for it would strand every pending one. Taken before the refusal above was added.
        spec = self.gateway._registry.get("harness.start_convergence")
        past_ascii = {
            "contract": {**CONTRACT, "metadata": {"na" + chr(0xEF) + "ve": "caf" + chr(0xE9), "emoji": chr(0x1F600)}},
            "mock_final_observation": {"url": "https://example.com/done", "k" + chr(0xE9) + "y": {chr(0xF1): 1}},
        }
        digests = {
            "e1296a272b601198aa0ee5185d60ca741f15df91000aa23ae5ebc8828cbc39f1": {"contract": CONTRACT},
            "75e802a824e4d74232bcecf73700ec3e079b68e2b3e4a65d02d475c01b368c4a": {
                "contract": CONTRACT,
                "session_id": "session-1",
                "provider": "claude",
                "max_attempts": 3,
            },
            "30027aaf96663129adfc783409dc629a844bdfbd2799894bed9aa4410a6c6215": past_ascii,
        }
        for digest, arguments in digests.items():
            with self.subTest(digest=digest):
                model = spec.input_model.model_validate(arguments)

                decision, _reason = McpToolGateway._governed_call_decision(spec, model)

                self.assertEqual(decision.text, f"harness.start_convergence sha256:{digest}")
