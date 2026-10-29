"""What the agent and the trail are told when the approval guard refuses an action before it begins.

effect_started refuses an action whose approval was released, already used, or
rejected while it waited. Those refusals reached the agent and the audit log as
"Action blocked by policy", so an agent could not tell its approval was spent
from a policy block; and an upload refused there was rolled back with a
navigation, as if it had run.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.action_errors import BrowserActionError
from app.approvals import ApprovalRequiredError, held_for_execution, refused_before_effect
from tests.test_approval_spend import OFF_LIST_URL, SESSION, START_URL, WAIT_SECONDS, SpendBase


class RefusalCodeTests(SpendBase):
    def hold_snapshot(self) -> tuple[asyncio.Event, asyncio.Event]:
        """Make the next pipeline run wait just before its effect, until ``go`` is set."""
        reached, go = asyncio.Event(), asyncio.Event()

        async def snapshot(*_: Any, **__: Any) -> dict[str, Any]:
            if not go.is_set():
                reached.set()
                await go.wait()
            return {"url": START_URL}

        self.manager._light_snapshot = snapshot  # type: ignore[method-assign]
        return reached, go

    async def assert_refused(self, refused: BrowserActionError, code: str, says: str) -> None:
        self.assertEqual((refused.code, refused.status_code), (code, 409), refused.message)
        self.assertIn(says, refused.message)
        events = await self.manager.list_audit_events(limit=50, event_type="browser_action")
        blocked = [event["details"] for event in events if event["status"] == "blocked"]
        self.assertIn(says, " ".join(str(details.get("error")) for details in blocked))
        # The code too, so an auditor can tell the approval guard from a policy block without the prose.
        self.assertIn(code, [details.get("code") for details in blocked])

    async def test_a_second_operation_is_refused_as_approval_already_used(self) -> None:
        decision, approval_id = await self.approved_click()

        async with held_for_execution(self.manager.approvals, approval_id):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
            with self.assertRaises(BrowserActionError) as ctx:
                await self.manager.click(SESSION, selector="#pay")

        await self.assert_refused(ctx.exception, "approval_already_used", "it covers one action")

    async def test_a_released_hold_is_refused_as_approval_released(self) -> None:
        decision, approval_id = await self.approved_click()
        reached, go = self.hold_snapshot()
        refusal = PermissionError("refused before the operation")

        with self.assertRaises(PermissionError):
            async with held_for_execution(self.manager.approvals, approval_id, may_give_back=True):
                child = asyncio.create_task(self.manager.execute_decision(SESSION, decision, approval_id=approval_id))
                await asyncio.wait_for(reached.wait(), WAIT_SECONDS)
                refused_before_effect(refusal)
                raise refusal
        go.set()
        with self.assertRaises(BrowserActionError) as ctx:
            await child

        await self.assert_refused(ctx.exception, "approval_released", "was released before this action began")

    async def test_a_rejected_hold_is_refused_as_approval_rejected(self) -> None:
        decision, approval_id = await self.approved_click()
        reached, go = self.hold_snapshot()
        task = asyncio.create_task(self.manager.execute_decision(SESSION, decision, approval_id=approval_id))
        await asyncio.wait_for(reached.wait(), WAIT_SECONDS)
        await self.manager.reject(approval_id, comment="no")

        go.set()
        with self.assertRaises(BrowserActionError) as ctx:
            await task

        await self.assert_refused(ctx.exception, "approval_rejected", "was rejected by an operator")


class UploadRefusalRollbackTests(SpendBase):
    async def test_a_policy_block_is_audited_with_its_code(self) -> None:
        decision, approval_id = await self.approved_click()
        self.page.url = OFF_LIST_URL  # refused before the click begins

        with self.assertRaises(BrowserActionError) as ctx:
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)

        events = await self.manager.list_audit_events(limit=50, event_type="browser_action")
        blocked = [event["details"].get("code") for event in events if event["status"] == "blocked"]
        self.assertEqual((ctx.exception.code, blocked), ("browser_action_blocked", ["browser_action_blocked"]))

    async def test_an_upload_refused_before_its_transfer_does_not_navigate_back(self) -> None:
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.upload(SESSION, selector="#file", file_path="demo.txt", approved=False)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id)
        go_backs: list[str] = []

        async def counted_go_back(**_: object) -> None:
            go_backs.append(self.page.url)
            self.page.url = START_URL

        self.page.go_back = counted_go_back  # type: ignore[method-assign]
        reached, go = asyncio.Event(), asyncio.Event()

        async def snapshot(*_: Any, **__: Any) -> dict[str, Any]:
            if not go.is_set():
                reached.set()
                await go.wait()
            return {"url": START_URL}

        self.manager._light_snapshot = snapshot  # type: ignore[method-assign]
        upload = {"selector": "#file", "file_path": "demo.txt", "approved": False, "approval_id": approval_id}
        refusal = PermissionError("refused before the operation")

        with self.assertRaises(PermissionError):
            async with held_for_execution(self.manager.approvals, approval_id, may_give_back=True):
                child = asyncio.create_task(self.manager.upload(SESSION, **upload))
                await asyncio.wait_for(reached.wait(), WAIT_SECONDS)
                refused_before_effect(refusal)
                raise refusal
        self.page.url = "https://example.com/next"  # the page moved itself, on an allowed host
        go.set()
        with self.assertRaises(BrowserActionError):
            await child

        self.assertEqual((go_backs, self.page.uploads), ([], []))

    async def test_an_upload_blocked_after_its_transfer_still_rolls_its_navigation_back(self) -> None:
        self.settings.require_approval_for_uploads = False
        go_backs: list[str] = []

        async def counted_go_back(**_: object) -> None:
            go_backs.append(self.page.url)
            self.page.url = START_URL

        page = self.page

        class NavigatingInput:
            """A file input whose page moves off the allowed hosts once the file is in."""

            first: Any = None

            async def set_input_files(self, path: str) -> None:
                page.uploads.append(path)
                page.url = OFF_LIST_URL

        file_input = NavigatingInput()
        file_input.first = file_input
        self.page.go_back = counted_go_back  # type: ignore[method-assign]
        self.page.locator = lambda _selector: file_input  # type: ignore[method-assign]

        with self.assertRaises(BrowserActionError):
            await self.manager.upload(SESSION, selector="#file", file_path="demo.txt", approved=False)

        self.assertEqual((go_backs, len(self.page.uploads)), ([OFF_LIST_URL], 1))
