from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from pydantic import ValidationError

from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import BrowserActionDecision, CreateSessionRequest, HoverRequest, SelectOptionRequest, WaitRequest
from app.utils import UTC


class RequestModelTests(unittest.TestCase):
    def test_hover_request_accepts_selector(self) -> None:
        req = HoverRequest(selector="#menu")
        self.assertEqual(req.selector, "#menu")
        self.assertIsNone(req.x)
        self.assertIsNone(req.y)

    def test_hover_request_accepts_coordinates(self) -> None:
        req = HoverRequest(x=100.0, y=200.0)
        self.assertIsNone(req.selector)
        self.assertEqual(req.x, 100.0)
        self.assertEqual(req.y, 200.0)

    def test_hover_request_all_optional(self) -> None:
        # HoverRequest itself has no required fields — BrowserManager validates the combination
        req = HoverRequest()
        self.assertIsNone(req.selector)

    def test_wait_request_clamps_min(self) -> None:
        req = WaitRequest(wait_ms=0)
        self.assertEqual(req.wait_ms, 0)

    def test_wait_request_clamps_max(self) -> None:
        with self.assertRaises(Exception):
            WaitRequest(wait_ms=31000)

    def test_wait_request_default(self) -> None:
        req = WaitRequest()
        self.assertEqual(req.wait_ms, 0)

    def test_select_option_request_value(self) -> None:
        req = SelectOptionRequest(selector="select#size", value="medium")
        self.assertEqual(req.selector, "select#size")
        self.assertEqual(req.value, "medium")
        self.assertIsNone(req.label)

    def test_select_option_request_index_non_negative(self) -> None:
        with self.assertRaises(Exception):
            SelectOptionRequest(selector="select", index=-1)


class BrowserActionDecisionExtendedTests(unittest.TestCase):
    def test_hover_requires_a_target(self) -> None:
        with self.assertRaises(ValidationError):
            BrowserActionDecision(action="hover", reason="Hover the menu")

    def test_select_option_requires_locator_and_choice(self) -> None:
        with self.assertRaises(ValidationError):
            BrowserActionDecision(action="select_option", reason="Choose an option", selector="#size")

        decision = BrowserActionDecision(
            action="select_option",
            reason="Choose medium",
            selector="#size",
            value="medium",
        )
        self.assertEqual(decision.risk_category, "write")

    def test_wait_defaults_to_read_risk(self) -> None:
        decision = BrowserActionDecision(action="wait", reason="Wait for the page to settle", wait_ms=1500)

        self.assertEqual(decision.risk_category, "read")
        self.assertEqual(decision.wait_ms, 1500)

    def test_navigation_shortcuts_default_to_read_risk(self) -> None:
        for action in ("reload", "go_back", "go_forward"):
            with self.subTest(action=action):
                decision = BrowserActionDecision(action=action, reason=f"Run {action}")
                self.assertEqual(decision.risk_category, "read")

    def test_social_write_actions_default_to_post_risk(self) -> None:
        post = BrowserActionDecision(action="social_post", reason="Share an update", text="hello world")
        comment = BrowserActionDecision(action="social_comment", reason="Reply to the post", text="nice post")
        like = BrowserActionDecision(action="social_like", reason="Like the first post")
        follow = BrowserActionDecision(action="social_follow", reason="Follow this profile")
        unfollow = BrowserActionDecision(action="social_unfollow", reason="Unfollow this profile")
        repost = BrowserActionDecision(action="social_repost", reason="Repost this update")
        dm = BrowserActionDecision(action="social_dm", reason="DM the creator", recipient="alice", text="hello")

        self.assertEqual(post.risk_category, "post")
        self.assertEqual(comment.risk_category, "post")
        self.assertEqual(like.risk_category, "post")
        self.assertEqual(follow.risk_category, "post")
        self.assertEqual(unfollow.risk_category, "post")
        self.assertEqual(repost.risk_category, "post")
        self.assertEqual(dm.risk_category, "post")

    def test_social_post_requires_text(self) -> None:
        with self.assertRaises(ValidationError):
            BrowserActionDecision(action="social_post", reason="Share an update")

    def test_social_dm_requires_recipient_and_text(self) -> None:
        with self.assertRaises(ValidationError):
            BrowserActionDecision(action="social_dm", reason="Send a DM", text="hello")

        with self.assertRaises(ValidationError):
            BrowserActionDecision(action="social_dm", reason="Send a DM", recipient="alice")

    def test_social_login_defaults_to_account_change_and_requires_identity(self) -> None:
        decision = BrowserActionDecision(
            action="social_login",
            reason="Log into X",
            platform="x",
            username="alice",
        )
        self.assertEqual(decision.risk_category, "account_change")

        with self.assertRaises(ValidationError):
            BrowserActionDecision(action="social_login", reason="Log into X", platform="x")

    def test_create_session_rejects_auth_profile_and_storage_state_together(self) -> None:
        with self.assertRaises(ValidationError):
            CreateSessionRequest(storage_state_path="state.json", auth_profile="outlook-default")

    def test_text_target_payload_redacts_sensitive_values(self) -> None:
        redacted = BrowserManager._text_target_payload(
            {"selector": "#password"},
            "secret-password",
            clear_first=True,
            sensitive=True,
            preview_chars=80,
        )
        self.assertTrue(redacted["text_redacted"])
        self.assertNotIn("text_preview", redacted)

        visible = BrowserManager._text_target_payload(
            {"selector": "#search"},
            "playwright",
            clear_first=True,
            sensitive=False,
            preview_chars=80,
        )
        self.assertEqual(visible["text_preview"], "playwright")


class FakeDownload:
    def __init__(self, suggested_filename: str, url: str, content: str = "downloaded") -> None:
        self.suggested_filename = suggested_filename
        self.url = url
        self._content = content

    async def save_as(self, path: str) -> None:
        Path(path).write_text(self._content, encoding="utf-8")

    async def failure(self) -> str | None:
        return None


class BrowserDownloadCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.settings = Settings(
            _env_file=None,
            ARTIFACT_ROOT=str(root / "artifacts"),
            AUTH_ROOT=str(root / "auth"),
            UPLOAD_ROOT=str(root / "uploads"),
            APPROVAL_ROOT=str(root / "approvals"),
            AUDIT_ROOT=str(root / "audit"),
            SESSION_STORE_ROOT=str(root / "sessions"),
        )
        self.manager = BrowserManager(self.settings)
        artifact_dir = Path(self.settings.artifact_root) / "session-1"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "downloads").mkdir(parents=True, exist_ok=True)
        self.session = BrowserSession(
            id="session-1",
            name="session-1",
            created_at=datetime.now(UTC),
            context=object(),  # type: ignore[arg-type]
            page=object(),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(self.settings.auth_root) / "session-1",
            upload_dir=Path(self.settings.upload_root) / "session-1",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        self.session.auth_dir.mkdir(parents=True, exist_ok=True)
        self.session.upload_dir.mkdir(parents=True, exist_ok=True)
        self.manager.sessions[self.session.id] = self.session
        self.manager._persist_session = AsyncMock()  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_handle_download_persists_session_artifact(self) -> None:
        await self.manager._handle_download(
            self.session,
            FakeDownload("report.csv", "https://example.com/report.csv", content="a,b\n1,2\n"),
        )

        downloads = await self.manager.list_downloads(self.session.id)
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0]["status"], "completed")
        self.assertTrue(Path(downloads[0]["path"]).is_file())
        self.assertEqual(downloads[0]["filename"], "report.csv")
        self.assertEqual(downloads[0]["url"], "/artifacts/session-1/downloads/report.csv")
        self.manager._persist_session.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
