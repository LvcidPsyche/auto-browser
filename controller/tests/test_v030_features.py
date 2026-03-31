"""Tests for auto-browser v0.3.0 features.

Covers:
- Perception presets (fast/normal/rich)
- SSE event bus subscribe/unsubscribe/emit
- Webhook signing (HMAC-SHA256)
- Screenshot diff helper
- New config settings
- New models (ObserveRequest, ImportAuthProfileRequest, ScreenshotDiffResponse)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import events as _events
from app.config import Settings
from app.models import (
    ImportAuthProfileRequest,
    ObserveRequest,
    ScreenshotDiffResponse,
)
from app.webhooks import _sign

# ── Perception Preset Models ─────────────────────────────────────────────────

class TestObserveRequest(unittest.TestCase):
    def test_default_preset_is_normal(self) -> None:
        req = ObserveRequest()
        self.assertEqual(req.preset, "normal")

    def test_fast_preset(self) -> None:
        req = ObserveRequest(preset="fast")
        self.assertEqual(req.preset, "fast")

    def test_rich_preset(self) -> None:
        req = ObserveRequest(preset="rich")
        self.assertEqual(req.preset, "rich")

    def test_default_limit(self) -> None:
        req = ObserveRequest()
        self.assertEqual(req.limit, 40)

    def test_custom_limit(self) -> None:
        req = ObserveRequest(limit=80)
        self.assertEqual(req.limit, 80)

    def test_limit_max(self) -> None:
        req = ObserveRequest(limit=200)
        self.assertEqual(req.limit, 200)

    def test_limit_too_large_raises(self) -> None:
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            ObserveRequest(limit=201)

    def test_invalid_preset_raises(self) -> None:
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            ObserveRequest(preset="turbo")  # type: ignore[arg-type]


class TestImportAuthProfileRequest(unittest.TestCase):
    def test_defaults(self) -> None:
        req = ImportAuthProfileRequest(archive_path="/tmp/foo.tar.gz")
        self.assertEqual(req.archive_path, "/tmp/foo.tar.gz")
        self.assertFalse(req.overwrite)

    def test_overwrite(self) -> None:
        req = ImportAuthProfileRequest(archive_path="/tmp/foo.tar.gz", overwrite=True)
        self.assertTrue(req.overwrite)


class TestScreenshotDiffResponse(unittest.TestCase):
    def test_construction(self) -> None:
        r = ScreenshotDiffResponse(
            changed_pixels=1000,
            changed_pct=0.5,
            diff_url="/artifacts/abc/diff.png",
            diff_path="/data/artifacts/abc/diff.png",
            a_url="/artifacts/abc/before.png",
            b_url="/artifacts/abc/after.png",
            width=1280,
            height=800,
        )
        self.assertEqual(r.changed_pixels, 1000)
        self.assertAlmostEqual(r.changed_pct, 0.5)


# ── SSE Event Bus ─────────────────────────────────────────────────────────────

class TestEventBus(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Clear state between tests
        _events._SESSION_QUEUES.clear()
        _events._GLOBAL_QUEUES.clear()

    async def test_subscribe_and_receive(self) -> None:
        q = _events.subscribe("sess-1")
        _events.emit_observe("sess-1", "https://example.com", "Example", None)
        payload = json.loads(q.get_nowait())
        self.assertEqual(payload["event"], "observe")
        self.assertEqual(payload["session_id"], "sess-1")
        self.assertEqual(payload["url"], "https://example.com")

    async def test_unsubscribe_stops_delivery(self) -> None:
        q = _events.subscribe("sess-2")
        _events.unsubscribe("sess-2", q)
        _events.emit_action("sess-2", "click", "ok")
        self.assertTrue(q.empty())

    async def test_global_subscriber_receives_all_sessions(self) -> None:
        gq = _events.subscribe_all()
        _events.emit_action("sess-a", "navigate", "ok")
        _events.emit_action("sess-b", "click", "ok")
        events = [json.loads(gq.get_nowait()) for _ in range(2)]
        session_ids = {e["session_id"] for e in events}
        self.assertIn("sess-a", session_ids)
        self.assertIn("sess-b", session_ids)
        _events.unsubscribe_all(gq)

    async def test_emit_approval_event(self) -> None:
        q = _events.subscribe("sess-3")
        _events.emit_approval("sess-3", "appr-1", "upload", "pending", "Upload needs approval")
        payload = json.loads(q.get_nowait())
        self.assertEqual(payload["event"], "approval")
        self.assertEqual(payload["approval_id"], "appr-1")
        self.assertEqual(payload["kind"], "upload")

    async def test_emit_session_event(self) -> None:
        q = _events.subscribe("sess-4")
        _events.emit_session("sess-4", "closed")
        payload = json.loads(q.get_nowait())
        self.assertEqual(payload["event"], "session")
        self.assertEqual(payload["status"], "closed")

    async def test_full_queue_drops_events_gracefully(self) -> None:
        q = _events.subscribe("sess-5")
        # Overflow the queue (maxsize=200)
        for i in range(210):
            _events.emit_action("sess-5", "click", "ok")
        # Should not raise — just drop extras
        self.assertLessEqual(q.qsize(), 200)
        _events.unsubscribe("sess-5", q)

    async def test_cleanup_empty_session_key(self) -> None:
        q = _events.subscribe("sess-6")
        _events.unsubscribe("sess-6", q)
        self.assertNotIn("sess-6", _events._SESSION_QUEUES)


# ── Webhook Signing ────────────────────────────────────────────────────────────

class TestWebhookSigning(unittest.TestCase):
    def test_sign_produces_sha256_prefix(self) -> None:
        sig = _sign(b'{"test":1}', "secret123")
        self.assertTrue(sig.startswith("sha256="))

    def test_sign_is_deterministic(self) -> None:
        payload = b'{"approval_id":"abc"}'
        sig1 = _sign(payload, "mysecret")
        sig2 = _sign(payload, "mysecret")
        self.assertEqual(sig1, sig2)

    def test_different_secrets_produce_different_sigs(self) -> None:
        payload = b'{"approval_id":"abc"}'
        sig1 = _sign(payload, "secret1")
        sig2 = _sign(payload, "secret2")
        self.assertNotEqual(sig1, sig2)

    def test_sign_matches_manual_hmac(self) -> None:
        payload = b'hello'
        secret = "webhook-secret"
        expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        self.assertEqual(_sign(payload, secret), expected)


# ── Config: New Settings ─────────────────────────────────────────────────────

class TestNewConfigSettings(unittest.TestCase):
    def test_approval_webhook_url_default_none(self) -> None:
        s = Settings()
        self.assertIsNone(s.approval_webhook_url)

    def test_approval_webhook_secret_default_none(self) -> None:
        s = Settings()
        self.assertIsNone(s.approval_webhook_secret)

    def test_perception_preset_default_normal(self) -> None:
        s = Settings()
        self.assertEqual(s.perception_preset_default, "normal")

    def test_sse_keepalive_default(self) -> None:
        s = Settings()
        self.assertAlmostEqual(s.sse_keepalive_seconds, 15.0)

    def test_approval_webhook_url_from_env(self) -> None:
        with patch.dict(os.environ, {"APPROVAL_WEBHOOK_URL": "https://hooks.example.com/1"}):
            s = Settings()
            self.assertEqual(s.approval_webhook_url, "https://hooks.example.com/1")

    def test_perception_preset_from_env(self) -> None:
        with patch.dict(os.environ, {"PERCEPTION_PRESET_DEFAULT": "fast"}):
            s = Settings()
            self.assertEqual(s.perception_preset_default, "fast")


# ── Auth Export / Import ─────────────────────────────────────────────────────

def _get_browser_manager_cls():
    """Import BrowserManager, patching missing optional deps."""
    import sys
    # Patch pytesseract if not installed
    if "pytesseract" not in sys.modules:
        sys.modules["pytesseract"] = MagicMock()
    from app.browser_manager import BrowserManager
    return BrowserManager


class TestAuthExportImport(unittest.TestCase):
    def test_export_creates_tar_gz(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth_root = Path(tmp)
            profile_dir = auth_root / "my-profile"
            profile_dir.mkdir()
            (profile_dir / "cookies.json").write_text('{"test": true}')

            BrowserManager = _get_browser_manager_cls()
            BrowserManager._write_tar(profile_dir, auth_root / "my-profile.tar.gz")

            archive = auth_root / "my-profile.tar.gz"
            self.assertTrue(archive.exists())
            with tarfile.open(str(archive), "r:gz") as t:
                names = t.getnames()
            self.assertTrue(any("my-profile" in n for n in names))

    def test_import_extracts_profile(self) -> None:
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            profile_dir = Path(src_tmp) / "test-profile"
            profile_dir.mkdir()
            (profile_dir / "state.json").write_text('{"session": "abc"}')

            BrowserManager = _get_browser_manager_cls()
            archive_path = Path(src_tmp) / "test-profile.tar.gz"
            BrowserManager._write_tar(profile_dir, archive_path)

            with tarfile.open(str(archive_path), "r:gz") as t:
                t.extractall(path=dst_tmp)

            extracted = Path(dst_tmp) / "test-profile" / "state.json"
            self.assertTrue(extracted.exists())
            self.assertIn("abc", extracted.read_text())


# ── Screenshot Diff ────────────────────────────────────────────────────────────

class TestScreenshotDiff(unittest.TestCase):
    def _make_png(self, path: Path, color: tuple[int, int, int] = (0, 0, 0)) -> None:
        try:
            from PIL import Image
            img = Image.new("RGB", (100, 100), color=color)
            img.save(str(path))
        except ImportError:
            self.skipTest("Pillow not installed")

    def test_identical_images_have_zero_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._make_png(d / "a.png", color=(0, 128, 255))
            self._make_png(d / "b.png", color=(0, 128, 255))
            BrowserManager = _get_browser_manager_cls()
            result = BrowserManager._compute_diff(
                str(d / "a.png"), str(d / "b.png"), "/a.png", "/b.png", d
            )
            self.assertEqual(result["changed_pixels"], 0)
            self.assertEqual(result["changed_pct"], 0.0)

    def test_different_images_have_nonzero_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._make_png(d / "a.png", color=(0, 0, 0))
            self._make_png(d / "b.png", color=(255, 255, 255))
            BrowserManager = _get_browser_manager_cls()
            result = BrowserManager._compute_diff(
                str(d / "a.png"), str(d / "b.png"), "/a.png", "/b.png", d
            )
            self.assertGreater(result["changed_pixels"], 0)
            self.assertGreater(result["changed_pct"], 0.0)
            self.assertIsNotNone(result["diff_url"])

    def test_missing_image_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            BrowserManager = _get_browser_manager_cls()
            result = BrowserManager._compute_diff(
                "/nonexistent/a.png", "/nonexistent/b.png", "/a.png", "/b.png", d
            )
            self.assertIn("error", result)
            self.assertEqual(result["changed_pixels"], -1)


if __name__ == "__main__":
    unittest.main()
