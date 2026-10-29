"""Who may use an approval whose session has no record: the operator the approval recorded.

An approval's access follows its session's owner. A session with no record
(its record lost while the approval stayed) has no owner to ask, and read as
unowned, so any operator could read, list, decide and execute another
operator's approval, and read its tool call's arguments. Such an approval now
follows the owner it recorded. A session that is known and owned by nobody
stays open to everyone, as before.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.models import OperatorIdentity
from app.session_ownership import acting_as
from tests.test_approval_spend import SESSION, SpendBase

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")


class OwnerFallbackTests(SpendBase):
    async def ask(self, operator: OperatorIdentity) -> str:
        """A governed tool call's approval, asked by ``operator`` in the session."""
        with acting_as(operator):
            asked = await self.call(
                "browser.eval_js", session_id=SESSION, expression="pay()", workflow_profile="governed"
            )
        self.assertEqual(asked.structuredContent.get("status"), "approval_required", asked.content[0].text)
        return asked.structuredContent["approval_id"]

    def reads(self, approval_id: str) -> dict[str, Callable[[], Awaitable[Any]]]:
        """Every way an operator reads or uses an approval by its id."""
        return {
            "get": lambda: self.manager.get_approval(approval_id),
            "arguments": lambda: self.manager.approval_arguments(approval_id),
            "approve": lambda: self.manager.approve(approval_id, comment="mine now"),
            "reject": lambda: self.manager.reject(approval_id, comment="mine now"),
            "execute": lambda: self.manager.execute_approval(approval_id),
        }

    async def listed(self, operator: OperatorIdentity, approval_id: str) -> bool:
        with acting_as(operator):
            return approval_id in [item["id"] for item in await self.manager.list_approvals()]

    async def test_an_approval_whose_session_has_no_record_follows_who_asked(self) -> None:
        self.session.owner = "alice"
        approval_id = await self.ask(ALICE)
        del self.manager.sessions[SESSION]  # the approval stays; its session is no longer known

        for name, read in self.reads(approval_id).items():
            with self.subTest(read=name), acting_as(BOB), self.assertRaises(KeyError):
                await read()
        self.assertFalse(await self.listed(BOB, approval_id))

        with acting_as(ALICE):
            self.assertEqual((await self.manager.get_approval(approval_id))["status"], "pending")
            self.assertIn("pay()", (await self.manager.approval_arguments(approval_id))["arguments"])
        self.assertTrue(await self.listed(ALICE, approval_id))

    async def test_a_known_session_nobody_owns_stays_open_to_everyone(self) -> None:
        approval_id = await self.ask(ALICE)  # the session has no owner

        with acting_as(BOB):
            self.assertEqual((await self.manager.get_approval(approval_id))["status"], "pending")
        self.assertTrue(await self.listed(BOB, approval_id))
