"""An approval id presented on an action that needs none is shown, but never as an approval the action required."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.browser_manager import BrowserManager, BrowserSession
from app.models import BrowserActionDecision
from app.utils import UTC
from tests.test_browser_manager_actions import FakeContext, FakePage, _settings


async def _manager(tmp: Path) -> BrowserManager:
    settings = _settings(tmp)
    settings.witness_enabled = True
    settings.pii_scrub_screenshot = False
    settings.pii_scrub_action_screenshots = False
    manager = BrowserManager(settings)
    manager._persist_session = AsyncMock()
    manager.artifacts.trace_payload = Mock(return_value={})
    manager._observation_payload = AsyncMock(return_value={"url": "https://example.com", "title": "t"})
    manager._light_snapshot = AsyncMock(return_value={"url": "https://example.com", "title": "t"})
    page = FakePage()
    artifact_dir = tmp / "artifacts" / "session-1"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for name in ("auth", "uploads"):
        (tmp / name / "session-1").mkdir(parents=True, exist_ok=True)
    manager.sessions["session-1"] = BrowserSession(
        id="session-1",
        name="f",
        created_at=datetime.now(UTC),
        context=FakeContext(page),
        page=page,
        artifact_dir=artifact_dir,
        auth_dir=tmp / "auth" / "session-1",
        upload_dir=tmp / "uploads" / "session-1",
        takeover_url="http://localhost:6080/vnc.html",
        trace_path=artifact_dir / "trace.zip",
    )
    return manager


def _receipts(tmp: Path) -> list[dict]:
    return [json.loads(line) for f in (tmp / "witness").rglob("*.jsonl") for line in f.read_text().splitlines()]


class UncheckedApprovalIdTests(unittest.IsolatedAsyncioTestCase):
    async def _receipt_for(self, **kwargs) -> dict:
        with tempfile.TemporaryDirectory() as tempdir:
            tmp = Path(tempdir)
            manager = await _manager(tmp)
            decision = BrowserActionDecision(action="click", reason="plain click", selector="#ok", risk_category="read")
            self.assertIsNone(manager.actions.approval_kind_for_decision(decision))
            with patch("asyncio.sleep", new=AsyncMock()):
                await manager.execute_decision("session-1", decision, **kwargs)
            receipts = _receipts(tmp)
            self.assertEqual(len(receipts), 1)
            return receipts[0]

    async def test_a_presented_id_on_a_no_approval_action_is_shown_with_no_status_and_not_required(self) -> None:
        plain = await self._receipt_for()
        bogus = await self._receipt_for(approval_id="apr_BOGUS_deadbeef")

        self.assertEqual(plain["approval"]["approval_id"], None)
        self.assertEqual(bogus["approval"]["approval_id"], "apr_BOGUS_deadbeef")
        self.assertIsNone(bogus["approval"]["status"])
        self.assertFalse(bogus["approval"]["required"])


if __name__ == "__main__":
    unittest.main()
