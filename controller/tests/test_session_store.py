from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import SessionRecord
from app.utils import UTC


class FakeTracing:
    async def stop(self, path: str | None = None) -> None:
        return None


class FakeContext:
    def __init__(self) -> None:
        self.tracing = FakeTracing()
        self.saved_storage_paths: list[str] = []

    async def close(self) -> None:
        return None

    async def storage_state(self, path: str) -> None:
        self.saved_storage_paths.append(path)
        Path(path).write_text("{}", encoding="utf-8")


class FakePage:
    def __init__(self, url: str = "https://example.com") -> None:
        self.url = url

    async def title(self) -> str:
        return "Example Domain"


class SessionStoreTests(unittest.IsolatedAsyncioTestCase):
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
        self.settings.enable_tracing = False
        self.manager = BrowserManager(self.settings)
        await self.manager.session_store.startup()

    async def asyncTearDown(self) -> None:
        await self.manager.session_store.shutdown()
        self.tempdir.cleanup()

    async def test_file_store_persists_and_interrupts_active_records(self) -> None:
        record = SessionRecord(
            id="session-1",
            name="session-1",
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
            status="active",
            live=True,
            current_url="https://example.com",
            title="Example Domain",
            artifact_dir="/tmp/session-1",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            remote_access={"active": False},
            last_action=None,
            trace_path="/tmp/session-1/trace.zip",
        )
        await self.manager.session_store.upsert(record)

        loaded = await self.manager.session_store.get("session-1")
        self.assertEqual(loaded.status, "active")
        self.assertTrue(loaded.live)

        await self.manager.session_store.mark_all_active_interrupted()

        interrupted = await self.manager.session_store.get("session-1")
        self.assertEqual(interrupted.status, "interrupted")
        self.assertFalse(interrupted.live)

    async def test_close_session_persists_closed_record_and_list_sessions_merges_store(self) -> None:
        artifact_dir = Path(self.settings.artifact_root) / "session-live"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        live_session = BrowserSession(
            id="session-live",
            name="session-live",
            created_at=datetime.now(UTC),
            context=FakeContext(),  # type: ignore[arg-type]
            page=FakePage("https://example.com/live"),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(self.settings.auth_root) / "session-live",
            upload_dir=Path(self.settings.upload_root) / "session-live",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        live_session.auth_dir.mkdir(parents=True, exist_ok=True)
        live_session.upload_dir.mkdir(parents=True, exist_ok=True)
        self.manager.sessions[live_session.id] = live_session

        archived = SessionRecord(
            id="session-archived",
            name="session-archived",
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
            status="interrupted",
            live=False,
            current_url="https://example.com/archived",
            title="Archived",
            artifact_dir="/tmp/session-archived",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            remote_access={"active": False},
            last_action="click",
            trace_path="/tmp/session-archived/trace.zip",
        )
        await self.manager.session_store.upsert(archived)

        sessions = await self.manager.list_sessions()
        session_ids = {item["id"] for item in sessions}
        self.assertIn("session-live", session_ids)
        self.assertIn("session-archived", session_ids)

        close_payload = await self.manager.close_session("session-live")
        self.assertTrue(close_payload["closed"])

        persisted = await self.manager.get_session_record("session-live")
        self.assertEqual(persisted["status"], "closed")
        self.assertFalse(persisted["live"])
        self.assertEqual(persisted["isolation"]["mode"], "shared_browser_node")
        auth_state = await self.manager.get_auth_state_info("session-live")
        self.assertEqual(auth_state["session_auth_root"], str(live_session.auth_dir))

    async def test_session_scoped_paths_prefer_session_roots(self) -> None:
        artifact_dir = Path(self.settings.artifact_root) / "session-scope"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        context = FakeContext()
        session = BrowserSession(
            id="session-scope",
            name="session-scope",
            created_at=datetime.now(UTC),
            context=context,  # type: ignore[arg-type]
            page=FakePage("https://example.com/scope"),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(self.settings.auth_root) / "session-scope",
            upload_dir=Path(self.settings.upload_root) / "session-scope",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        session.auth_dir.mkdir(parents=True, exist_ok=True)
        session.upload_dir.mkdir(parents=True, exist_ok=True)
        self.manager.sessions[session.id] = session
        self.manager._append_jsonl = AsyncMock()  # type: ignore[method-assign]

        session_file = session.upload_dir / "demo.txt"
        session_file.write_text("session copy", encoding="utf-8")
        global_file = Path(self.settings.upload_root) / "demo.txt"
        global_file.parent.mkdir(parents=True, exist_ok=True)
        global_file.write_text("global copy", encoding="utf-8")

        resolved = self.manager._safe_upload_path("demo.txt", session=session)
        self.assertEqual(resolved, session_file.resolve())

        payload = await self.manager.save_storage_state(session.id, "state.json")
        self.assertIn("/session-scope/state.json", payload["saved_to"])

    async def test_save_auth_profile_persists_reusable_profile(self) -> None:
        artifact_dir = Path(self.settings.artifact_root) / "session-profile"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        context = FakeContext()
        session = BrowserSession(
            id="session-profile",
            name="session-profile",
            created_at=datetime.now(UTC),
            context=context,  # type: ignore[arg-type]
            page=FakePage("https://outlook.live.com/mail/0/"),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(self.settings.auth_root) / "session-profile",
            upload_dir=Path(self.settings.upload_root) / "session-profile",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        session.auth_dir.mkdir(parents=True, exist_ok=True)
        session.upload_dir.mkdir(parents=True, exist_ok=True)
        self.manager.sessions[session.id] = session
        self.manager._append_jsonl = AsyncMock()  # type: ignore[method-assign]

        payload = await self.manager.save_auth_profile(session.id, "outlook-default")

        self.assertEqual(payload["profile_name"], "outlook-default")
        self.assertIn("/profiles/outlook-default/state.json", payload["saved_to"])
        profile = await self.manager.get_auth_profile("outlook-default")
        self.assertEqual(profile["profile_name"], "outlook-default")
        self.assertEqual(profile["metadata"]["saved_from_session_id"], session.id)
        self.assertEqual(profile["metadata"]["platform"], "outlook")
        self.assertEqual(session.auth_profile_name, "outlook-default")

        profiles = await self.manager.list_auth_profiles()
        self.assertEqual([item["profile_name"] for item in profiles], ["outlook-default"])
