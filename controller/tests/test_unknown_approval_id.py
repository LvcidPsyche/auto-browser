"""A tool call that presents an approval id no record answers to is told so.

It was answered with the id alone, the text of the store's KeyError: nothing said the approval was unknown, or what
to do next. The retention deletes decided approvals (APPROVAL_RETENTION_DAYS), so an agent that kept an old id met
this in ordinary use.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from tests.test_approval_spend import SESSION, SpendBase

UNKNOWN = "0123456789ab"
COOKIE = {"name": "sid", "value": "v", "url": "https://example.com/"}
REPEAT = "Repeat the call without approval_id to ask for a new approval."
ASK_AGAIN = "To get a new approval, repeat the call that needed one without approval_id."


class UnknownApprovalIdTests(SpendBase):
    async def _written(self) -> tuple[int, int, int]:
        approvals = len(await self.manager.approvals.list())
        audit = len(await self.manager.audit.list(limit=1000))
        receipts = len(await self.manager.list_witness_receipts(SESSION))
        return approvals, audit, receipts

    async def _assert_told(self, name: str, what_next: str, **arguments) -> str:
        before = await self._written()

        answer = await self.call(name, approval_id=UNKNOWN, **arguments)

        text = answer.content[0].text
        self.assertTrue(answer.isError)
        self.assertTrue(text.startswith(f"Unknown approval '{UNKNOWN}': it never existed, or it was deleted"), text)
        self.assertIn("APPROVAL_RETENTION_DAYS (30)", text)
        self.assertTrue(text.endswith(what_next), text)
        self.assertEqual(answer.structuredContent, {"error": text})
        self.assertEqual(await self._written(), before, "a call with an unknown approval id wrote something")
        return text

    async def test_a_governed_call_that_presents_an_unknown_id(self) -> None:
        await self._assert_told(
            "browser.set_cookies", REPEAT, session_id=SESSION, workflow_profile="governed", cookies=[COOKIE]
        )

    async def test_executing_an_unknown_id(self) -> None:
        await self._assert_told("browser.execute_approval", ASK_AGAIN)

    async def test_rejecting_an_unknown_id(self) -> None:
        await self._assert_told("browser.reject_approval", ASK_AGAIN)

    async def test_approving_an_unknown_id_where_agents_may_approve(self) -> None:
        self.settings.autonomous_approvals = True

        await self._assert_told("browser.approve_approval", ASK_AGAIN)

    async def test_with_the_retention_off_nothing_is_said_to_have_been_deleted(self) -> None:
        self.settings.approval_retention_days = 0

        answer = await self.call("browser.execute_approval", approval_id=UNKNOWN)

        self.assertEqual(answer.content[0].text, f"Unknown approval '{UNKNOWN}': it never existed. {ASK_AGAIN}")

    async def test_an_approval_of_another_operator_reads_as_an_unknown_one(self) -> None:
        # Who may use an approval is the manager's answer, a KeyError for a stranger as for no record: the same text.
        _decision, approval_id = await self.approved_click()
        self.manager.approval_service._may_use = AsyncMock(return_value=False)  # type: ignore[method-assign]

        theirs = await self.call("browser.execute_approval", approval_id=approval_id)
        unknown = await self.call("browser.execute_approval", approval_id=UNKNOWN)

        self.assertTrue(theirs.isError)
        self.assertEqual(theirs.content[0].text.replace(approval_id, UNKNOWN), unknown.content[0].text)

    async def test_another_missing_thing_keeps_its_own_words(self) -> None:
        # Only a KeyError that names the presented id is the approval's.
        self.manager.execute_approval = AsyncMock(side_effect=KeyError("Memory profile not found"))  # type: ignore[method-assign]

        answer = await self.call("browser.execute_approval", approval_id=UNKNOWN)

        self.assertEqual(answer.content[0].text, "Memory profile not found")
