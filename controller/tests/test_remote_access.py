from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.utils import UTC


class FakePage:
    def __init__(self, url: str = "https://example.com"):
        self.url = url
        self.accessibility = self.FakeAccessibility()

    class FakeAccessibility:
        async def snapshot(self, interesting_only: bool = True):
            return {
                "role": "WebArea",
                "name": "Example Domain",
                "children": [
                    {"role": "heading", "name": "Example Domain"},
                    {"role": "link", "name": "More information", "focused": True},
                ],
            }

    async def title(self) -> str:
        return "Example Domain"

    async def evaluate(self, script: str, arg=None):
        if "document.querySelectorAll(selector)" in script:
            return [
                {
                    "element_id": "op-123",
                    "label": "More information",
                    "role": "link",
                    "tag": "a",
                    "type": None,
                    "disabled": False,
                    "href": "https://www.iana.org/domains/example",
                    "bbox": {"x": 8, "y": 16, "width": 120, "height": 24},
                    "selector_hint": '[data-operator-id="op-123"]',
                }
            ]
        if "text_excerpt" in script and "dom_outline" in script:
            return {
                "text_excerpt": "Example Domain. More information.",
                "dom_outline": {
                    "headings": [{"level": "h1", "text": "Example Domain"}],
                    "forms": [],
                    "counts": {"links": 1, "buttons": 0, "inputs": 0, "forms": 0},
                },
            }
        return {
            "tag": "body",
            "element_id": None,
            "name": None,
            "id": None,
            "label": "",
        }


class RemoteAccessInfoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.info_path = root / "tunnels" / "reverse-ssh.json"
        self.settings = Settings(_env_file=None)
        self.settings.artifact_root = str(root / "artifacts")
        self.settings.upload_root = str(root / "uploads")
        self.settings.auth_root = str(root / "auth")
        self.settings.approval_root = str(root / "approvals")
        self.settings.session_store_root = str(root / "sessions")
        self.settings.audit_root = str(root / "audit")
        self.settings.takeover_url = "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale"
        self.settings.remote_access_info_path = str(self.info_path)
        self.settings.remote_access_stale_after_seconds = 30
        self.manager = BrowserManager(self.settings)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_remote_access_defaults_without_metadata(self) -> None:
        info = self.manager.get_remote_access_info()
        self.assertFalse(info["active"])
        self.assertEqual(info["status"], "inactive")
        self.assertFalse(info["exists"])
        self.assertEqual(info["takeover_url"], self.settings.takeover_url)
        self.assertIsNone(info["api_url"])

    def test_remote_access_uses_fresh_metadata(self) -> None:
        self.info_path.parent.mkdir(parents=True, exist_ok=True)
        self.info_path.write_text(
            json.dumps(
                {
                    "status": "active",
                    "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "stale_after_seconds": 20,
                    "public_api_url": "http://bastion.example.com:18000",
                    "public_takeover_url": "http://bastion.example.com:16080/vnc.html",
                }
            ),
            encoding="utf-8",
        )
        info = self.manager.get_remote_access_info()
        self.assertTrue(info["active"])
        self.assertEqual(info["status"], "active")
        self.assertEqual(info["api_url"], "http://bastion.example.com:18000")
        self.assertEqual(info["takeover_url"], "http://bastion.example.com:16080/vnc.html")
        self.assertFalse(info["stale"])
        self.assertIsNotNone(info["last_updated"])

    def test_remote_access_marks_stale_metadata(self) -> None:
        self.info_path.parent.mkdir(parents=True, exist_ok=True)
        self.info_path.write_text(
            json.dumps(
                {
                    "status": "active",
                    "updated_at": (
                        datetime.now(UTC) - timedelta(seconds=90)
                    ).isoformat().replace("+00:00", "Z"),
                    "stale_after_seconds": 10,
                    "public_api_url": "http://bastion.example.com:18000",
                    "public_takeover_url": "http://bastion.example.com:16080/vnc.html",
                }
            ),
            encoding="utf-8",
        )
        info = self.manager.get_remote_access_info()
        self.assertFalse(info["active"])
        self.assertTrue(info["stale"])
        self.assertEqual(info["status"], "stale")
        self.assertEqual(info["takeover_url"], self.settings.takeover_url)
        self.assertIsNone(info["api_url"])

    def test_remote_access_handles_malformed_metadata(self) -> None:
        self.info_path.parent.mkdir(parents=True, exist_ok=True)
        self.info_path.write_text("{not-json", encoding="utf-8")
        info = self.manager.get_remote_access_info()
        self.assertFalse(info["active"])
        self.assertEqual(info["status"], "error")
        self.assertEqual(info["takeover_url"], self.settings.takeover_url)
        self.assertIsNotNone(info["error"])


class RemoteAccessPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.info_path = root / "tunnels" / "reverse-ssh.json"
        self.settings = Settings(_env_file=None)
        self.settings.artifact_root = str(root / "artifacts")
        self.settings.upload_root = str(root / "uploads")
        self.settings.auth_root = str(root / "auth")
        self.settings.approval_root = str(root / "approvals")
        self.settings.session_store_root = str(root / "sessions")
        self.settings.audit_root = str(root / "audit")
        self.settings.takeover_url = "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale"
        self.settings.remote_access_info_path = str(self.info_path)
        self.settings.remote_access_stale_after_seconds = 30
        self.manager = BrowserManager(self.settings)
        self.info_path.parent.mkdir(parents=True, exist_ok=True)
        self.info_path.write_text(
            json.dumps(
                {
                    "status": "active",
                    "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "stale_after_seconds": 20,
                    "public_api_url": "http://bastion.example.com:18000",
                    "public_takeover_url": "http://bastion.example.com:16080/vnc.html?autoconnect=true",
                }
            ),
            encoding="utf-8",
        )
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
            takeover_url="http://internal-only:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        self.session.auth_dir.mkdir(parents=True, exist_ok=True)
        self.session.upload_dir.mkdir(parents=True, exist_ok=True)
        self.manager.sessions[self.session.id] = self.session
        self.manager._capture_screenshot = AsyncMock(
            return_value={"path": "/tmp/test.png", "url": "/artifacts/session-1/test.png"}
        )
        self.manager._append_jsonl = AsyncMock()

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_tunnel_url_propagates_into_session_summary_observe_and_takeover(self) -> None:
        summary = await self.manager._session_summary(self.session)
        self.assertEqual(
            summary["takeover_url"],
            "http://bastion.example.com:16080/vnc.html?autoconnect=true",
        )
        self.assertTrue(summary["remote_access"]["active"])

        observation = await self.manager._observation_payload(self.session, limit=10)
        self.assertEqual(
            observation["takeover_url"],
            "http://bastion.example.com:16080/vnc.html?autoconnect=true",
        )
        self.assertEqual(observation["remote_access"]["status"], "active")
        self.assertTrue(observation["accessibility_outline"]["available"])
        self.assertEqual(observation["accessibility_outline"]["focused"]["role"], "link")

        takeover = await self.manager.request_human_takeover(self.session.id, "Need login review")
        self.assertEqual(
            takeover["takeover_url"],
            "http://bastion.example.com:16080/vnc.html?autoconnect=true",
        )
        self.assertTrue(takeover["remote_access"]["active"])
