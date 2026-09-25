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

from app.approvals import SENSITIVE_TEXT_PREFIX, ApprovalStore
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

    async def test_non_sensitive_text_is_kept_for_the_approver_to_read(self) -> None:
        approval = await self.request(typing("Hello team", sensitive=False))
        self.assertEqual(approval.action.text, "Hello team")


if __name__ == "__main__":
    unittest.main()
