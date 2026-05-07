from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from app.approvals import ApprovalRequiredError, ApprovalStore
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import BrowserActionDecision
from app.utils import UTC


class FakePage:
    def __init__(self, url: str = "https://example.com"):
        self.url = url

    async def title(self) -> str:
        return "Example Domain"


class ApprovalQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.settings = Settings(_env_file=None)
        self.settings.artifact_root = str(root / "artifacts")
        self.settings.upload_root = str(root / "uploads")
        self.settings.auth_root = str(root / "auth")
        self.settings.approval_root = str(root / "approvals")
        self.settings.session_store_root = str(root / "sessions")
        self.settings.audit_root = str(root / "audit")
        self.manager = BrowserManager(self.settings)

        artifact_dir = Path(self.settings.artifact_root) / "session-1"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.session = BrowserSession(
            id="session-1",
            name="session-1",
            created_at=datetime.now(UTC),
            context=object(),  # type: ignore[arg-type]
            page=FakePage(),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(self.settings.auth_root) / "session-1",
            upload_dir=Path(self.settings.upload_root) / "session-1",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        self.session.auth_dir.mkdir(parents=True, exist_ok=True)
        self.session.upload_dir.mkdir(parents=True, exist_ok=True)
        self.manager.sessions[self.session.id] = self.session

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_upload_requires_pending_approval_then_executes(self) -> None:
        upload_path = Path(self.settings.upload_root) / "demo.txt"
        upload_path.parent.mkdir(parents=True, exist_ok=True)
        upload_path.write_text("demo", encoding="utf-8")

        self.manager._run_action = AsyncMock(return_value={"action": "upload"})  # type: ignore[method-assign]

        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.upload(
                self.session.id,
                selector='input[type="file"]',
                file_path="demo.txt",
                approved=False,
            )

        approval = ctx.exception.approval
        self.assertEqual(approval.kind, "upload")
        self.assertEqual(approval.status, "pending")

        await self.manager.approve(approval.id, comment="looks good")
        result = await self.manager.upload(
            self.session.id,
            selector='input[type="file"]',
            file_path="demo.txt",
            approved=False,
            approval_id=approval.id,
        )

        self.assertEqual(result["action"], "upload")
        stored = await self.manager.get_approval(approval.id)
        self.assertEqual(stored["status"], "executed")

    async def test_sensitive_decision_creates_queue_item_and_execute_approval_runs_action(self) -> None:
        self.manager.click = AsyncMock(return_value={"action": "click"})  # type: ignore[method-assign]

        decision = BrowserActionDecision(
            action="click",
            reason="This button submits a payment",
            element_id="op-pay",
            risk_category="payment",
        )

        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.execute_decision(self.session.id, decision)

        approval = ctx.exception.approval
        self.assertEqual(approval.kind, "payment")
        await self.manager.approve(approval.id, comment="approved")

        result = await self.manager.execute_approval(approval.id)

        self.assertEqual(result["approval"]["status"], "executed")
        self.manager.click.assert_awaited_once_with(
            self.session.id,
            selector=None,
            element_id="op-pay",
            x=None,
            y=None,
        )

    async def test_governed_write_decision_requires_write_approval(self) -> None:
        decision = BrowserActionDecision(
            action="click",
            reason="Click the save button",
            element_id="op-save",
            risk_category="write",
        )

        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.require_governed_approval(
                self.session.id,
                decision,
                approval_id=None,
            )

        approval = ctx.exception.approval
        self.assertEqual(approval.kind, "write")
        self.assertEqual(approval.action.action, "click")
        await self.manager.approve(approval.id, comment="operator approved")

        approved = await self.manager.require_governed_approval(
            self.session.id,
            decision,
            approval_id=approval.id,
        )

        self.assertEqual(approved.id, approval.id)

class ApprovalStoreSQLiteTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_store_persists_and_filters_records(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "approvals"
            db_path = Path(tempdir) / "db" / "operator.db"
            store = ApprovalStore(root, db_path=str(db_path))
            await store.startup()

            action = BrowserActionDecision(
                action="click",
                reason="Submit payment",
                element_id="op-pay",
                risk_category="payment",
            )
            approval = await store.create_or_reuse_pending(
                session_id="session-1",
                kind="payment",
                reason="Payment requires approval",
                action=action,
                observation={"url": "https://example.com"},
            )
            await store.approve(approval.id, comment="approved")

            loaded = await store.get(approval.id)
            self.assertEqual(loaded.status, "approved")

            approved = await store.list(status="approved")
            self.assertEqual(len(approved), 1)
            self.assertEqual(approved[0].id, approval.id)
            self.assertTrue(db_path.exists())

    async def test_expired_approval_is_rejected_for_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "approvals"
            store = ApprovalStore(root)
            await store.startup()

            action = BrowserActionDecision(
                action="click",
                reason="Submit payment",
                element_id="op-pay",
                risk_category="payment",
            )
            approval = await store.create_or_reuse_pending(
                session_id="session-1",
                kind="payment",
                reason="Payment requires approval",
                action=action,
                observation={"url": "https://example.com"},
            )
            approval = await store.approve(approval.id, comment="approved")
            approval.approved_expires_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
            await store.file_store.upsert(approval)

            with self.assertRaises(PermissionError):
                await store.require_approved(
                    approval_id=approval.id,
                    session_id="session-1",
                    kind="payment",
                    action=action,
                )

            with self.assertRaises(PermissionError):
                await store.mark_executed(approval.id)
