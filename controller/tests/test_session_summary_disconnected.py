from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.async_api import Error as PlaywrightError

from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.routes.sessions import create_sessions_router
from app.utils import UTC


class HealthyPage:
    def __init__(self, url: str = "https://example.com") -> None:
        self.url = url

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        return "Example Domain"


class ClosedPage:
    def __init__(self, url: str = "https://example.com") -> None:
        self.url = url

    def is_closed(self) -> bool:
        return True

    async def title(self) -> str:
        raise PlaywrightError("Target page, context or browser has been closed")


class TargetClosedPage:
    def __init__(self, url: str = "https://example.com") -> None:
        self.url = url

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        raise PlaywrightError("Target page, context or browser has been closed")


def _make_session(manager: BrowserManager, page: object, session_id: str = "session-1") -> BrowserSession:
    artifact_dir = Path(manager.settings.artifact_root) / session_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    session = BrowserSession(
        id=session_id,
        name=session_id,
        created_at=datetime.now(UTC),
        context=object(),  # type: ignore[arg-type]
        page=page,  # type: ignore[arg-type]
        artifact_dir=artifact_dir,
        auth_dir=Path(manager.settings.auth_root) / session_id,
        upload_dir=Path(manager.settings.upload_root) / session_id,
        takeover_url=manager.settings.takeover_url,
        trace_path=artifact_dir / "trace.zip",
    )
    session.auth_dir.mkdir(parents=True, exist_ok=True)
    session.upload_dir.mkdir(parents=True, exist_ok=True)
    manager.sessions[session.id] = session
    return session


class SessionSummaryDisconnectedTests(unittest.IsolatedAsyncioTestCase):
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
        self.settings.takeover_url = "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale"
        self.manager = BrowserManager(self.settings)
        self.manager.session_store.list = AsyncMock(return_value=[])  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_summary_marks_closed_page_as_interrupted(self) -> None:
        session = _make_session(self.manager, ClosedPage())

        summary = await self.manager._session_summary(session)

        self.assertEqual(summary["status"], "interrupted")
        self.assertFalse(summary["live"])
        self.assertEqual(summary["current_url"], "")
        self.assertEqual(summary["title"], "")

    async def test_summary_survives_target_closed_error(self) -> None:
        session = _make_session(self.manager, TargetClosedPage())

        summary = await self.manager._session_summary(session)

        self.assertEqual(summary["status"], "interrupted")
        self.assertFalse(summary["live"])
        self.assertEqual(summary["current_url"], "")
        self.assertEqual(summary["title"], "")

    async def test_summary_keeps_explicit_closed_status(self) -> None:
        session = _make_session(self.manager, ClosedPage())

        summary = await self.manager._session_summary(session, status="closed", live=False)

        self.assertEqual(summary["status"], "closed")
        self.assertFalse(summary["live"])

    async def test_list_sessions_survives_disconnected_page(self) -> None:
        _make_session(self.manager, ClosedPage())

        sessions = await self.manager.list_sessions()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["status"], "interrupted")
        self.assertFalse(sessions[0]["live"])

    async def test_get_session_record_survives_disconnected_page(self) -> None:
        session = _make_session(self.manager, TargetClosedPage())

        record = await self.manager.get_session_record(session.id)

        self.assertEqual(record["status"], "interrupted")
        self.assertFalse(record["live"])

    async def test_healthy_page_summary_stays_active(self) -> None:
        session = _make_session(self.manager, HealthyPage())

        summary = await self.manager._session_summary(session)

        self.assertEqual(summary["status"], "active")
        self.assertTrue(summary["live"])
        self.assertEqual(summary["current_url"], "https://example.com")
        self.assertEqual(summary["title"], "Example Domain")


class SessionListRouteDisconnectedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.settings = Settings(_env_file=None)
        self.settings.artifact_root = str(root / "artifacts")
        self.settings.upload_root = str(root / "uploads")
        self.settings.auth_root = str(root / "auth")
        self.settings.approval_root = str(root / "approvals")
        self.settings.session_store_root = str(root / "sessions")
        self.settings.audit_root = str(root / "audit")
        self.settings.takeover_url = "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale"
        self.manager = BrowserManager(self.settings)
        self.manager.session_store.list = AsyncMock(return_value=[])  # type: ignore[method-assign]
        _make_session(self.manager, ClosedPage())
        app = FastAPI()
        app.include_router(create_sessions_router(manager=self.manager))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_list_sessions_route_returns_200_for_disconnected_page(self) -> None:
        response = self.client.get("/sessions")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["status"], "interrupted")
        self.assertFalse(payload[0]["live"])
