from __future__ import annotations

import asyncio
import tempfile
import threading
import time
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

    async def test_cleanup_keeps_a_new_empty_directory(self) -> None:
        """A session being created has fresh, empty directories before it is
        registered; the sweep removed them because they were empty."""
        new_dir = Path(self.settings.artifact_root) / "session-starting"
        new_dir.mkdir()

        await self.service.run_cleanup()

        self.assertTrue(new_dir.exists())

    async def test_cleanup_removes_a_stale_directory_it_emptied(self) -> None:
        stale_dir = Path(self.settings.artifact_root) / "session-old"
        stale_dir.mkdir()
        stale_file = stale_dir / "trace.zip"
        stale_file.write_text("old", encoding="utf-8")
        ancient = 1_000_000_000
        utime(stale_file, (ancient, ancient))
        utime(stale_dir, (ancient, ancient))

        report = await self.service.run_cleanup()

        self.assertFalse(stale_dir.exists())
        self.assertEqual(report["roots"][0]["deleted_dirs"], 1)

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

    async def test_cleanup_skips_saved_auth_profiles(self) -> None:
        profile_dir = Path(self.settings.auth_root) / "profiles" / "outlook-default"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_state = profile_dir / "state.json"
        profile_state.write_text("{}", encoding="utf-8")
        ancient = 1_000_000_000
        utime(profile_state, (ancient, ancient))

        report = await self.service.run_cleanup()

        self.assertTrue(profile_state.exists())
        self.assertGreaterEqual(report["roots"][2]["skipped_protected"], 1)

    async def test_cleanup_walks_the_filesystem_off_the_event_loop(self) -> None:
        """The sweep ran on the loop: 6000 stale files froze it for 13 s on NTFS."""
        loop_thread = threading.get_ident()
        walk_threads: list[int] = []
        real_cleanup_root = self.service._cleanup_root

        def slow_cleanup_root(*args, **kwargs):
            walk_threads.append(threading.get_ident())
            time.sleep(0.6)  # stands in for a long walk; blocks only its own thread
            return real_cleanup_root(*args, **kwargs)

        self.service._cleanup_root = slow_cleanup_root  # type: ignore[method-assign]
        gaps: list[float] = []
        stop = asyncio.Event()

        async def ticker() -> None:
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        await self.service.run_cleanup()
        stop.set()
        await task

        self.assertEqual(len(walk_threads), 3)
        self.assertNotIn(loop_thread, walk_threads)
        # Each blocked walk is 0.6 s; a loop that stayed free ticks every ~10 ms.
        # Half the walk is a wide margin for a loaded CI box.
        self.assertLess(max(gaps), 0.3)


if __name__ == "__main__":
    unittest.main()
