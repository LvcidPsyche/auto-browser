"""Each action's witness context belongs to that action only.

The context (risk, approval id) used to sit on the session until the pipeline took the session lock, and the
action's `finally` cleared it. An action that finished while another waited for the lock wiped the waiting
action's context, so its receipt lost its approval and risk.
"""

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager, BrowserSession
from app.models import BrowserActionDecision
from app.utils import UTC
from tests.test_browser_manager_actions import FakeContext, FakePage, _settings

# Long enough for a loaded machine; what the test waits on is an event, not a duration.
WAIT_SECONDS = 30


class SignallingLock(asyncio.Lock):
    """A session lock that says when a second action has come to wait for it: by then that action has done all it
    does before the lock (the old code had set its context on the session)."""

    def __init__(self) -> None:
        super().__init__()
        self.waited_on = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.waited_on.set()
        return await super().acquire()


class WitnessActionContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_finished_action_does_not_wipe_a_waiting_actions_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _settings(root)
            settings.witness_enabled = True
            manager = BrowserManager(settings)
            await manager.witness.startup()
            manager.audit.append = AsyncMock()
            manager._append_jsonl = AsyncMock()
            manager._persist_session = AsyncMock()
            page = FakePage()
            dirs = {name: root / name / "s1" for name in ("artifacts", "auth", "uploads")}
            for folder in dirs.values():
                folder.mkdir(parents=True)
            lock = SignallingLock()
            manager.sessions["s1"] = BrowserSession(
                id="s1",
                name="fake",
                created_at=datetime.now(UTC),
                context=FakeContext(page),
                page=page,
                artifact_dir=dirs["artifacts"],
                auth_dir=dirs["auth"],
                upload_dir=dirs["uploads"],
                takeover_url="http://x",
                trace_path=dirs["artifacts"] / "trace.zip",
                lock=lock,
            )
            manager._light_snapshot = AsyncMock(return_value={"url": "https://example.com", "title": "t"})
            manager._observation_payload = AsyncMock(
                return_value={"url": "https://example.com", "title": "t", "text_excerpt": "x"}
            )
            manager._settle = AsyncMock()
            manager._ensure_witness_remote_ready = AsyncMock()
            manager._check_bot_challenge = AsyncMock(return_value=None)
            manager.actions.require_decision_approval = AsyncMock(return_value=None)

            inside, release, first_seen = asyncio.Event(), asyncio.Event(), {"done": False}

            async def slow_click(session, locator, coords):
                if not first_seen["done"]:
                    first_seen["done"] = True
                    inside.set()
                    await release.wait()

            async def center(locator):
                return (1.0, 1.0)

            manager.actions.click_locator_human_like = slow_click
            manager.actions.locator_center = center

            first = BrowserActionDecision(action="click", selector="#a", reason="A", risk_category="payment")
            second = BrowserActionDecision(action="click", selector="#b", reason="B", risk_category="account_change")
            task_a = asyncio.create_task(manager.execute_decision("s1", first, approval_id="APPROVAL-A"))
            await asyncio.wait_for(inside.wait(), WAIT_SECONDS)  # A holds the session lock, mid-click
            task_b = asyncio.create_task(manager.execute_decision("s1", second, approval_id="APPROVAL-B"))
            await asyncio.wait_for(lock.waited_on.wait(), WAIT_SECONDS)  # B is now waiting for the lock
            release.set()
            await asyncio.wait_for(task_a, WAIT_SECONDS)
            await asyncio.wait_for(task_b, WAIT_SECONDS)

            receipts = await manager.witness.list("s1", limit=10)
            by_risk = {receipt.risk_category: receipt for receipt in receipts if receipt.action == "click"}
            self.assertEqual(by_risk["payment"].approval.approval_id, "APPROVAL-A")
            self.assertEqual(by_risk["account_change"].approval.approval_id, "APPROVAL-B")
            self.assertEqual(by_risk["account_change"].action_class, "account_change")


if __name__ == "__main__":
    unittest.main()
