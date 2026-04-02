from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.approvals import ApprovalRequiredError
from app.audit import reset_current_operator, set_current_operator
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import BrowserActionDecision
from app.utils import UTC
from app.witness import WitnessActionContext, WitnessPolicyEngine, WitnessRecorder, WitnessSessionContext


class WitnessCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = WitnessPolicyEngine()

    def test_normal_profile_warns_without_blocking(self) -> None:
        outcome = self.engine.evaluate_action(
            session=WitnessSessionContext(
                session_id="session-1",
                profile="normal",
                shared_takeover_surface=True,
                shared_browser_process=True,
                auth_state_encrypted=False,
                operator={"id": "anonymous", "source": "anonymous"},
            ),
            action=WitnessActionContext(action="upload", action_class="upload", runtime_requires_approval=True),
        )

        self.assertFalse(outcome.should_block)
        self.assertTrue(outcome.require_approval)

    def test_confidential_profile_blocks_anonymous_high_risk_action(self) -> None:
        outcome = self.engine.evaluate_action(
            session=WitnessSessionContext(
                session_id="session-1",
                profile="confidential",
                shared_takeover_surface=True,
                shared_browser_process=True,
                auth_state_encrypted=False,
                operator={"id": "anonymous", "source": "anonymous"},
            ),
            action=WitnessActionContext(action="social_post", action_class="post"),
        )

        self.assertTrue(outcome.should_block)
        self.assertTrue(any(concern.enforced for concern in outcome.concerns))


class WitnessRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def test_hash_chain_links_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            recorder = WitnessRecorder(Path(tempdir))
            await recorder.startup()

            first = await recorder.record(
                "session-1",
                profile="normal",
                event_type="browser_action",
                status="ok",
                action="click",
                action_class="write",
                operator={"id": "alice", "source": "header"},
            )
            second = await recorder.record(
                "session-1",
                profile="normal",
                event_type="browser_action",
                status="ok",
                action="type",
                action_class="write",
                operator={"id": "alice", "source": "header"},
            )

            self.assertEqual(second.chain_prev_hash, first.chain_hash)


class WitnessBrowserManagerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_confidential_auth_profile_save_is_blocked_without_encryption(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(root / "artifacts"),
                AUTH_ROOT=str(root / "auth"),
                UPLOAD_ROOT=str(root / "uploads"),
                APPROVAL_ROOT=str(root / "approvals"),
                AUDIT_ROOT=str(root / "audit"),
                WITNESS_ROOT=str(root / "witness"),
                SESSION_STORE_ROOT=str(root / "sessions"),
                REQUIRE_AUTH_STATE_ENCRYPTION="false",
            )
            manager = BrowserManager(settings)
            await manager.witness.startup()

            artifact_dir = Path(settings.artifact_root) / "session-1"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            session = BrowserSession(
                id="session-1",
                name="session-1",
                created_at=datetime.now(UTC),
                context=object(),  # type: ignore[arg-type]
                page=object(),  # type: ignore[arg-type]
                artifact_dir=artifact_dir,
                auth_dir=Path(settings.auth_root) / "session-1",
                upload_dir=Path(settings.upload_root) / "session-1",
                takeover_url="http://127.0.0.1:6080/vnc.html",
                trace_path=artifact_dir / "trace.zip",
                protection_mode="confidential",
                shared_takeover_surface=False,
                shared_browser_process=False,
            )
            session.auth_dir.mkdir(parents=True, exist_ok=True)
            session.upload_dir.mkdir(parents=True, exist_ok=True)
            manager.sessions[session.id] = session

            token = set_current_operator("alice", name="Alice")
            try:
                with self.assertRaises(PermissionError):
                    await manager.save_auth_profile(session.id, "confidential-profile")
            finally:
                reset_current_operator(token)

            receipts = await manager.list_witness_receipts(session.id, limit=10)
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["status"], "blocked")
            self.assertEqual(receipts[0]["action"], "save_auth_profile")

    async def test_approval_lifecycle_is_recorded_in_witness(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(root / "artifacts"),
                AUTH_ROOT=str(root / "auth"),
                UPLOAD_ROOT=str(root / "uploads"),
                APPROVAL_ROOT=str(root / "approvals"),
                AUDIT_ROOT=str(root / "audit"),
                WITNESS_ROOT=str(root / "witness"),
                SESSION_STORE_ROOT=str(root / "sessions"),
            )
            manager = BrowserManager(settings)
            await manager.approvals.startup()
            await manager.witness.startup()

            artifact_dir = Path(settings.artifact_root) / "session-1"
            artifact_dir.mkdir(parents=True, exist_ok=True)

            class _Page:
                url = "https://example.com"

                async def title(self) -> str:
                    return "Example"

            session = BrowserSession(
                id="session-1",
                name="session-1",
                created_at=datetime.now(UTC),
                context=object(),  # type: ignore[arg-type]
                page=_Page(),  # type: ignore[arg-type]
                artifact_dir=artifact_dir,
                auth_dir=Path(settings.auth_root) / "session-1",
                upload_dir=Path(settings.upload_root) / "session-1",
                takeover_url="http://127.0.0.1:6080/vnc.html",
                trace_path=artifact_dir / "trace.zip",
            )
            session.auth_dir.mkdir(parents=True, exist_ok=True)
            session.upload_dir.mkdir(parents=True, exist_ok=True)
            manager.sessions[session.id] = session
            manager.click = AsyncMock(return_value={"action": "click"})  # type: ignore[method-assign]

            token = set_current_operator("alice", name="Alice")
            try:
                with self.assertRaises(ApprovalRequiredError):
                    await manager.execute_decision(
                        session.id,
                        BrowserActionDecision(
                            action="click",
                            reason="Submit payment",
                            element_id="submit",
                            risk_category="payment",
                        ),
                    )
                pending = await manager.list_witness_receipts(session.id, limit=10)
                self.assertTrue(any(item["status"] == "pending" for item in pending))

                approval_id = pending[0]["approval"]["approval_id"]
                await manager.approve(approval_id, comment="approved")
                await manager.execute_approval(approval_id)
                receipts = await manager.list_witness_receipts(session.id, limit=20)
            finally:
                reset_current_operator(token)

            statuses = [item["status"] for item in receipts]
            self.assertIn("pending", statuses)
            self.assertIn("approved", statuses)
            self.assertIn("executed", statuses)

    async def test_normal_session_remote_witness_failure_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(root / "artifacts"),
                AUTH_ROOT=str(root / "auth"),
                UPLOAD_ROOT=str(root / "uploads"),
                APPROVAL_ROOT=str(root / "approvals"),
                AUDIT_ROOT=str(root / "audit"),
                WITNESS_ROOT=str(root / "witness"),
                SESSION_STORE_ROOT=str(root / "sessions"),
                WITNESS_REMOTE_URL="https://witness.example",
                WITNESS_REMOTE_REQUIRED_FOR_CONFIDENTIAL="true",
            )
            manager = BrowserManager(settings)
            await manager.witness.startup()

            artifact_dir = Path(settings.artifact_root) / "session-1"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            session = BrowserSession(
                id="session-1",
                name="session-1",
                created_at=datetime.now(UTC),
                context=object(),  # type: ignore[arg-type]
                page=object(),  # type: ignore[arg-type]
                artifact_dir=artifact_dir,
                auth_dir=Path(settings.auth_root) / "session-1",
                upload_dir=Path(settings.upload_root) / "session-1",
                takeover_url="http://127.0.0.1:6080/vnc.html",
                trace_path=artifact_dir / "trace.zip",
                protection_mode="normal",
                witness_remote_state=manager._initial_witness_remote_state("normal"),
            )
            session.auth_dir.mkdir(parents=True, exist_ok=True)
            session.upload_dir.mkdir(parents=True, exist_ok=True)

            manager.witness_remote.record = AsyncMock(side_effect=RuntimeError("remote down"))  # type: ignore[method-assign]

            token = set_current_operator("alice", name="Alice")
            try:
                await manager._record_witness_receipt(
                    session,
                    event_type="browser_action",
                    status="ok",
                    action="click",
                    action_class="write",
                    target={"selector": "#submit"},
                )
            finally:
                reset_current_operator(token)

            receipts = await manager.list_witness_receipts(session.id, limit=10)
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["action"], "click")
            self.assertFalse(session.witness_remote_state.required)
            self.assertEqual(session.witness_remote_state.status, "failed")
            self.assertEqual(session.witness_remote_state.last_error, "remote down")

    async def test_remote_forwarding_preserves_event_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(root / "artifacts"),
                AUTH_ROOT=str(root / "auth"),
                UPLOAD_ROOT=str(root / "uploads"),
                APPROVAL_ROOT=str(root / "approvals"),
                AUDIT_ROOT=str(root / "audit"),
                WITNESS_ROOT=str(root / "witness"),
                SESSION_STORE_ROOT=str(root / "sessions"),
                WITNESS_REMOTE_URL="https://witness.example",
            )
            manager = BrowserManager(settings)
            await manager.witness.startup()

            artifact_dir = Path(settings.artifact_root) / "session-1"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            session = BrowserSession(
                id="session-1",
                name="session-1",
                created_at=datetime.now(UTC),
                context=object(),  # type: ignore[arg-type]
                page=object(),  # type: ignore[arg-type]
                artifact_dir=artifact_dir,
                auth_dir=Path(settings.auth_root) / "session-1",
                upload_dir=Path(settings.upload_root) / "session-1",
                takeover_url="http://127.0.0.1:6080/vnc.html",
                trace_path=artifact_dir / "trace.zip",
                protection_mode="normal",
                witness_remote_state=manager._initial_witness_remote_state("normal"),
            )
            session.auth_dir.mkdir(parents=True, exist_ok=True)
            session.upload_dir.mkdir(parents=True, exist_ok=True)

            remote_record = AsyncMock(return_value={"ok": True})
            manager.witness_remote.record = remote_record  # type: ignore[method-assign]

            token = set_current_operator("alice", name="Alice")
            try:
                await manager._record_witness_receipt(
                    session,
                    event_type="browser_action",
                    status="ok",
                    action="click",
                    action_class="write",
                    target={"selector": "#submit"},
                )
            finally:
                reset_current_operator(token)

            remote_record.assert_awaited_once()
            forwarded_payload = remote_record.await_args.args[1]
            self.assertIsInstance(forwarded_payload.get("timestamp"), str)

    async def test_confidential_session_remote_witness_preflight_blocks_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(root / "artifacts"),
                AUTH_ROOT=str(root / "auth"),
                UPLOAD_ROOT=str(root / "uploads"),
                APPROVAL_ROOT=str(root / "approvals"),
                AUDIT_ROOT=str(root / "audit"),
                WITNESS_ROOT=str(root / "witness"),
                SESSION_STORE_ROOT=str(root / "sessions"),
                WITNESS_REMOTE_URL="https://witness.example",
                WITNESS_REMOTE_REQUIRED_FOR_CONFIDENTIAL="true",
            )
            manager = BrowserManager(settings)

            artifact_dir = Path(settings.artifact_root) / "session-1"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            session = BrowserSession(
                id="session-1",
                name="session-1",
                created_at=datetime.now(UTC),
                context=object(),  # type: ignore[arg-type]
                page=object(),  # type: ignore[arg-type]
                artifact_dir=artifact_dir,
                auth_dir=Path(settings.auth_root) / "session-1",
                upload_dir=Path(settings.upload_root) / "session-1",
                takeover_url="http://127.0.0.1:6080/vnc.html",
                trace_path=artifact_dir / "trace.zip",
                protection_mode="confidential",
                witness_remote_state=manager._initial_witness_remote_state("confidential"),
            )

            manager.witness_remote.healthz = AsyncMock(side_effect=RuntimeError("connection refused"))  # type: ignore[method-assign]

            with self.assertRaises(PermissionError):
                await manager._ensure_witness_remote_ready(session, action="save_auth_profile")

            self.assertTrue(session.witness_remote_state.required)
            self.assertEqual(session.witness_remote_state.status, "failed")
            self.assertIn("save_auth_profile", session.witness_remote_state.last_error or "")


if __name__ == "__main__":
    unittest.main()
