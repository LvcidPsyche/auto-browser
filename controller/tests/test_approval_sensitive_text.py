"""An approval never stores or serves the text of a sensitive `type`.

Approvals are written to APPROVAL_ROOT, listed by GET /approvals and the
approvals MCP tool, and shown to the approver. A password or card number typed
with sensitive=true was kept there verbatim, while the witness chain and action
logs already redacted it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.approvals import SENSITIVE_TEXT_PREFIX, SENSITIVE_TEXT_RETENTION, ApprovalStore
from app.models import BrowserActionDecision

SECRET = "hunter2-correct-horse"


def typing(text: str, *, sensitive: bool = True) -> BrowserActionDecision:
    return BrowserActionDecision(
        action="type",
        reason="Enter the card PIN",
        selector="#pin",
        text=text,
        sensitive=sensitive,
        risk_category="payment",
    )


class SensitiveApprovalTextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "approvals"
        self.store = ApprovalStore(self.root)
        await self.store.startup()

    async def request(self, action: BrowserActionDecision):
        return await self.store.create_or_reuse_pending(
            session_id="session-1", kind="payment", reason="needs approval", action=action
        )

    async def test_the_text_is_not_stored_or_listed(self) -> None:
        approval = await self.request(typing(SECRET))
        self.assertTrue(approval.action.text.startswith(SENSITIVE_TEXT_PREFIX))
        for listed in await self.store.list():
            self.assertNotIn(SECRET, listed.model_dump_json())
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))

    async def test_the_same_text_still_matches_and_other_text_does_not(self) -> None:
        approval = await self.request(typing(SECRET))
        self.assertEqual((await self.request(typing(SECRET))).id, approval.id)
        await self.store.approve(approval.id)

        await self.store.require_approved(
            approval_id=approval.id, session_id="session-1", kind="payment", action=typing(SECRET)
        )
        with self.assertRaises(PermissionError):
            await self.store.require_approved(
                approval_id=approval.id, session_id="session-1", kind="payment", action=typing("something-else")
            )

    async def test_execute_approval_gets_the_real_text_back(self) -> None:
        approval = await self.store.approve((await self.request(typing(SECRET))).id)
        executable = self.store.executable_action(approval)
        self.assertEqual(executable.text, SECRET)
        await self.store.require_approved(
            approval_id=approval.id, session_id="session-1", kind="payment", action=executable
        )

    async def test_after_a_restart_the_action_must_ask_again(self) -> None:
        approval = await self.store.approve((await self.request(typing(SECRET))).id)
        restarted = ApprovalStore(self.root)
        await restarted.startup()
        with self.assertRaises(PermissionError):
            restarted.executable_action(await restarted.get(approval.id))
        with self.assertRaises(PermissionError):
            await restarted.require_approved(
                approval_id=approval.id, session_id="session-1", kind="payment", action=typing(SECRET)
            )

    async def test_an_approve_of_text_no_longer_held_is_refused_before_the_record_changes(self) -> None:
        # A reject drops the text (a secret is not kept for a change of mind), and so does a restart: an
        # approve then gave an approval that execute_approval cannot run. The agent asks again instead.
        rejected = await self.store.reject((await self.request(typing(SECRET))).id, comment="no")
        pending = await self.request(typing("another-pin"))
        restarted = ApprovalStore(self.root)
        await restarted.startup()

        for store, approval in ((self.store, rejected), (restarted, pending)):
            with self.subTest(status=approval.status):
                with self.assertRaisesRegex(PermissionError, "its text is no longer held; the agent asks again"):
                    await store.approve(approval.id, comment="ok after all")
                self.assertEqual((await store.get(approval.id)).status, approval.status)

    async def test_undecided_text_is_dropped_after_the_retention_window(self) -> None:
        approval = await self.store.approve((await self.request(typing(SECRET))).id)
        text, kept_at = self.store._sensitive_texts[approval.id]
        self.store._sensitive_texts[approval.id] = (text, kept_at - SENSITIVE_TEXT_RETENTION.total_seconds() - 1)
        with self.assertRaises(PermissionError):
            self.store.executable_action(approval)
        self.assertNotIn(approval.id, self.store._sensitive_texts)

    async def test_asking_again_for_a_pending_approval_whose_text_aged_out_holds_the_text_again(self) -> None:
        """B-LC-2: the held text of a pending sensitive approval aged out (SENSITIVE_TEXT_RETENTION), so it
        could not be approved; asking again matched the same pending approval by its digest and returned it as
        it was, stuck for good."""
        approval = await self.request(typing(SECRET))
        text, kept_at = self.store._sensitive_texts[approval.id]
        self.store._sensitive_texts[approval.id] = (text, kept_at - SENSITIVE_TEXT_RETENTION.total_seconds() - 60)
        with self.assertRaisesRegex(PermissionError, "its text is no longer held"):
            await self.store.approve(approval.id)

        again = await self.request(typing(SECRET))
        approved = await self.store.approve(again.id)

        self.assertEqual((again.id, approved.status), (approval.id, "approved"))
        self.assertEqual(self.store.executable_action(approved).text, SECRET)

    async def test_non_sensitive_text_is_kept_for_the_approver_to_read(self) -> None:
        approval = await self.request(typing("Hello team", sensitive=False))
        self.assertEqual(approval.action.text, "Hello team")


if __name__ == "__main__":
    unittest.main()
