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


class StartupSweepInBackgroundTests(unittest.IsolatedAsyncioTestCase):
    """CLEANUP_ON_STARTUP's sweep runs after startup, not inside it: on a 20,000-file Docker Desktop bind mount the
    lifespan awaited it for ~150 s, and /readyz did not answer until it finished."""

    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.settings = Settings(
            _env_file=None,
            ARTIFACT_ROOT=str(root / "artifacts"),
            UPLOAD_ROOT=str(root / "uploads"),
            AUTH_ROOT=str(root / "auth"),
            CLEANUP_ON_STARTUP="true",
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

    def _block_the_walk(self) -> threading.Event:
        release = threading.Event()
        original = self.service._cleanup_root

        def slow(root, retention, protected):
            release.wait(5)
            return original(root, retention, protected)

        self.service._cleanup_root = slow  # type: ignore[method-assign]
        return release

    async def test_startup_returns_before_the_sweep_finishes(self) -> None:
        release = self._block_the_walk()
        started = time.monotonic()
        with self.assertLogs("app.maintenance", level="INFO") as logs:
            await asyncio.wait_for(self.service.startup(), timeout=1)
            self.assertLess(time.monotonic() - started, 1)
            self.assertIsNone(self.service.last_report)  # still running
            release.set()
            for _ in range(100):
                if self.service.last_report is not None:
                    break
                await asyncio.sleep(0.05)
        self.assertIsNotNone(self.service.last_report)
        text = "\n".join(logs.output)
        self.assertIn("startup cleanup sweep started", text)
        self.assertRegex(text, r"startup cleanup sweep finished in [0-9.]+s: \d+ files and \d+ dirs removed")

    async def test_shutdown_cancels_a_running_sweep_and_stops_its_walk(self) -> None:
        stale = Path(self.settings.artifact_root) / "old"
        stale.mkdir()
        for index in range(50):
            path = stale / f"{index}.txt"
            path.write_text("x", encoding="utf-8")
            utime(path, (1_000_000_000, 1_000_000_000))
        release = self._block_the_walk()
        await self.service.startup()
        task = self.service._startup_task
        self.assertIsNotNone(task)
        shutdown = asyncio.create_task(self.service.shutdown())
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(shutdown, timeout=5)
        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())
        # The walk saw the abort flag before deleting anything.
        await asyncio.sleep(0.2)
        self.assertEqual(len(list(stale.iterdir())), 50)

    async def test_the_background_sweep_skips_live_sessions(self) -> None:
        live = Path(self.settings.artifact_root) / "live-session"
        live.mkdir()
        old_file = live / "trace.zip"
        old_file.write_text("x", encoding="utf-8")
        utime(old_file, (1_000_000_000, 1_000_000_000))
        utime(live, (1_000_000_000, 1_000_000_000))
        self.active_sessions.append(ActiveSession(artifact_dir=live))
        await self.service.startup()
        await asyncio.wait_for(self.service._startup_task, timeout=5)
        self.assertTrue(old_file.exists())
        self.assertEqual(self.service.last_report["roots"][0]["skipped_protected"], 1)


@unittest.skipUnless((Path(__file__).resolve().parents[1] / "app" / "main.py").is_file(), "")
class LifespanWiringTests(unittest.TestCase):
    def test_the_lifespan_does_not_await_a_sweep(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "maintenance.py").read_text(encoding="utf-8")
        begin = source.index("    async def startup(self)")
        startup = source[begin : source.index("\n    async def ", begin + 1)]
        self.assertNotIn("await self.run_cleanup()", startup)
        self.assertIn("asyncio.create_task(self._startup_sweep()", startup)
