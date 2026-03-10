from __future__ import annotations

import tempfile
import unittest
from os import utime
from pathlib import Path

from app.config import Settings
from app.maintenance import MaintenanceService


class ActiveSession:
    def __init__(self, artifact_dir: Path | None = None, upload_dir: Path | None = None, auth_dir: Path | None = None):
        self.artifact_dir = artifact_dir
        self.upload_dir = upload_dir
        self.auth_dir = auth_dir


class MaintenanceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.settings = Settings(
            _env_file=None,
            ARTIFACT_ROOT=str(root / "artifacts"),
            UPLOAD_ROOT=str(root / "uploads"),
            AUTH_ROOT=str(root / "auth"),
            CLEANUP_ON_STARTUP="false",
            CLEANUP_INTERVAL_SECONDS="0",
            ARTIFACT_RETENTION_HOURS="1",
            UPLOAD_RETENTION_HOURS="1",
            AUTH_RETENTION_HOURS="1",
        )
        for path in (self.settings.artifact_root, self.settings.upload_root, self.settings.auth_root):
            Path(path).mkdir(parents=True, exist_ok=True)
        self.active_sessions: list[ActiveSession] = []
        self.service = MaintenanceService(self.settings, session_provider=lambda: self.active_sessions)

    async def asyncTearDown(self) -> None:
        await self.service.shutdown()
        self.tempdir.cleanup()

    async def test_cleanup_removes_stale_files(self) -> None:
        stale_file = Path(self.settings.artifact_root) / "old.txt"
        stale_file.write_text("old", encoding="utf-8")
        ancient = 1_000_000_000
        utime(stale_file, (ancient, ancient))

        report = await self.service.run_cleanup()

        self.assertFalse(stale_file.exists())
        self.assertEqual(report["roots"][0]["deleted_files"], 1)

    async def test_cleanup_skips_active_session_roots(self) -> None:
        protected_dir = Path(self.settings.auth_root) / "session-1"
        protected_dir.mkdir(parents=True, exist_ok=True)
        protected_file = protected_dir / "state.json.enc"
        protected_file.write_text("{}", encoding="utf-8")
        ancient = 1_000_000_000
        utime(protected_file, (ancient, ancient))
        self.active_sessions.append(ActiveSession(auth_dir=protected_dir))

        report = await self.service.run_cleanup()

        self.assertTrue(protected_file.exists())
        self.assertEqual(report["roots"][2]["skipped_protected"], 1)


if __name__ == "__main__":
    unittest.main()
