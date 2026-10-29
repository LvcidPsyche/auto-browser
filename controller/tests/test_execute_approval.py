"""Executing an approval by its id (POST /approvals/{id}/execute, browser.execute_approval) answers as the other paths do.

It claimed an upload approval before checking its file, so a file that had gone
spent the approval on not_found, where browser.execute_action and the agent loop
check first and keep it. And it answered a spent approval "is not approved",
not with the spent refusal the other paths give.
"""

from __future__ import annotations

from app.approvals import ApprovalRequiredError
from tests.test_approval_spend import SESSION, SpendBase


class ExecuteApprovalTests(SpendBase):
    async def test_an_upload_whose_file_went_missing_keeps_its_approval(self) -> None:
        staged = self.session.upload_dir / "later.txt"
        staged.write_text("later", encoding="utf-8")
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.upload(SESSION, selector="#file", file_path="later.txt", approved=False)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id)
        staged.unlink()

        with self.assertRaises(FileNotFoundError):
            await self.manager.execute_approval(approval_id)

        self.assertEqual((self.page.uploads, await self.status(approval_id)), ([], "approved"))

    async def test_a_spent_approval_is_refused_as_spent(self) -> None:
        decision, approval_id = await self.approved_click()
        await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        executed_at = (await self.manager.approvals.get(approval_id)).executed_at

        with self.assertRaises(PermissionError) as refused:
            await self.manager.execute_approval(approval_id)

        self.assertIn(f"was spent when its action started (executed_at {executed_at})", str(refused.exception))
        self.assertEqual(self.page.clicks, 1)

    async def test_its_audit_and_receipt_name_who_built_the_approval(self) -> None:
        _, approval_id = await self.approved_click()

        await self.manager.execute_approval(approval_id)

        executed = await self.manager.list_audit_events(limit=50, event_type="approval_executed")
        receipts = [
            item
            for item in await self.manager.list_witness_receipts(SESSION)
            if item["event_type"] == "approval" and item["action"] == "execute_approval"
        ]
        self.assertEqual([event["details"].get("origin") for event in executed], ["action"])
        self.assertEqual([item["target"].get("origin") for item in receipts], ["action"])
