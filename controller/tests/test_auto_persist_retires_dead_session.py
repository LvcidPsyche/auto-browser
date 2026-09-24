from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from playwright.async_api import Error as PlaywrightError

from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.utils import UTC


class ClosedPage:
    """A page whose tab has closed out from under the session (site closed its
    own window, crash, etc.) while the rest of the browser stays alive -- the
    scenario that used to leave a session stuck in `manager.sessions` forever.
    """

    def __init__(self) -> None:
        self.url = "https://example.com"

    def is_closed(self) -> bool:
        return True

    async def title(self) -> str:
        raise PlaywrightError("Target page, context or browser has been closed")


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        ARTIFACT_ROOT=str(root / "artifacts"),
        UPLOAD_ROOT=str(root / "uploads"),
        AUTH_ROOT=str(root / "auth"),
        APPROVAL_ROOT=str(root / "approvals"),
        AUDIT_ROOT=str(root / "audit"),
        WITNESS_ROOT=str(root / "witness"),
        SESSION_STORE_ROOT=str(root / "sessions"),
        MAX_SESSIONS=1,
        AUTO_PERSIST_INTERVAL_SECONDS=0,
    )


def _make_session(manager: BrowserManager, session_id: str = "zombie-1") -> BrowserSession:
    artifact_dir = Path(manager.settings.artifact_root) / session_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    auth_dir = Path(manager.settings.auth_root) / session_id
    upload_dir = Path(manager.settings.upload_root) / session_id
    auth_dir.mkdir(parents=True, exist_ok=True)
    upload_dir.mkdir(parents=True, exist_ok=True)
    context = AsyncMock()
    context.close = AsyncMock()
    session = BrowserSession(
        id=session_id,
        name=session_id,
        created_at=datetime.now(UTC),
        context=context,
        page=ClosedPage(),  # type: ignore[arg-type]
        artifact_dir=artifact_dir,
        auth_dir=auth_dir,
        upload_dir=upload_dir,
        takeover_url=manager.settings.takeover_url,
        trace_path=artifact_dir / "trace.zip",
        auto_persist_profile_name="owner-default",
    )
    manager.sessions[session.id] = session
    return session


class AutoPersistRetiresDeadSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.manager = BrowserManager(_settings(root))
        self.manager.session_store.list = AsyncMock(return_value=[])  # type: ignore[method-assign]
        self.manager.session_store.upsert = AsyncMock()  # type: ignore[method-assign]
        self.manager.audit.append = AsyncMock()

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_dead_page_is_evicted_and_frees_the_session_slot(self) -> None:
        session = _make_session(self.manager)
        # Every real save attempt against a dead page fails this way (see
        # AuthProfiles.save_for_session, which calls `page.title()`).
        self.manager.auth_profiles.save_auto_persist = AsyncMock(
            side_effect=PlaywrightError("Target page, context or browser has been closed")
        )

        self.assertIn(session.id, self.manager.sessions)
        # max_sessions=1 and the zombie occupies the only slot: this is the
        # exact production symptom -- every subsequent Open refused forever.
        with self.assertRaises(RuntimeError):
            self.manager._check_session_limit()

        task = asyncio.create_task(
            self.manager.session_lifecycle._auto_persist_loop(session, "owner-default")
        )
        await asyncio.wait_for(task, timeout=2)

        self.assertNotIn(session.id, self.manager.sessions)
        session.context.close.assert_awaited()
        # The slot is free again -- a new Open can proceed.
        self.manager._check_session_limit()

    async def test_retire_is_idempotent_if_session_already_gone(self) -> None:
        session = _make_session(self.manager)
        self.manager.sessions.pop(session.id)  # e.g. an explicit close raced us

        await self.manager.session_lifecycle._retire_dead_session(session, reason="test")

        session.context.close.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
