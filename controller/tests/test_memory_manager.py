from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import memory_manager
from app.memory_manager import MemoryManager, MemoryProfile


class MemoryManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name) / "memory"
        self.mem = MemoryManager(self.root)

    async def test_startup_creates_dir(self) -> None:
        await self.mem.startup()
        self.assertTrue(self.root.is_dir())

    async def test_save_and_get(self) -> None:
        await self.mem.startup()
        profile = await self.mem.save(
            "test-profile",
            goal_summary="Login to dashboard",
            completed_steps=["navigated to /login", "entered credentials"],
            discovered_selectors={"login_btn": "#submit"},
        )
        self.assertEqual(profile.name, "test-profile")

        loaded = await self.mem.get("test-profile")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.goal_summary, "Login to dashboard")
        self.assertIn("login_btn", loaded.discovered_selectors)

    async def test_get_nonexistent(self) -> None:
        await self.mem.startup()
        self.assertIsNone(await self.mem.get("does-not-exist"))

    async def test_save_merges_steps(self) -> None:
        await self.mem.startup()
        await self.mem.save("p", completed_steps=["step1"])
        await self.mem.save("p", completed_steps=["step2"])
        profile = await self.mem.get("p")
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertIn("step1", profile.completed_steps)
        self.assertIn("step2", profile.completed_steps)

    async def test_list(self) -> None:
        await self.mem.startup()
        await self.mem.save("alpha")
        await self.mem.save("beta")
        profiles = await self.mem.list()
        names = [profile["name"] for profile in profiles]
        self.assertIn("alpha", names)
        self.assertIn("beta", names)

    async def test_delete(self) -> None:
        await self.mem.startup()
        await self.mem.save("to-delete")
        self.assertTrue(await self.mem.delete("to-delete"))
        self.assertIsNone(await self.mem.get("to-delete"))

    async def test_delete_nonexistent(self) -> None:
        await self.mem.startup()
        self.assertFalse(await self.mem.delete("ghost"))


class MemoryProfileTests(unittest.TestCase):
    def test_to_system_prompt(self) -> None:
        profile = MemoryProfile(
            name="p",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            goal_summary="Do the thing",
            completed_steps=["step a"],
            discovered_selectors={"btn": "#submit"},
        )

        prompt = profile.to_system_prompt()

        self.assertIn("Do the thing", prompt)
        self.assertIn("step a", prompt)
        self.assertIn("#submit", prompt)


class MemoryProfileNameCollisionTests(unittest.IsolatedAsyncioTestCase):
    """Two names that map to one file are two profiles, not one.

    File names replace every character outside [A-Za-z0-9_-] with "_", so
    "github.com" and "github_com" were both github_com.json: saving one merged
    into the other, and reading one returned the other's notes.
    """

    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = MemoryManager(Path(self.tempdir.name))
        await self.manager.startup()
        await self.manager.save("github.com", notes=["note for github.com"])

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_saving_a_colliding_name_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            await self.manager.save("github_com", notes=["note for github_com"])
        profile = await self.manager.get("github.com")
        self.assertEqual(profile.notes, ["note for github.com"])

    async def test_reading_a_colliding_name_finds_nothing(self) -> None:
        self.assertIsNone(await self.manager.get("github_com"))

    async def test_deleting_a_colliding_name_leaves_the_other_profile(self) -> None:
        self.assertFalse(await self.manager.delete("github_com"))
        self.assertIsNotNone(await self.manager.get("github.com"))


class UnreadableProfileTests(unittest.IsolatedAsyncioTestCase):
    """STORE-36: a file that is no profile broke the whole listing, and failed get, save and delete with an error that
    quoted the file; a delete did not wait for a save of the same profile."""

    async def asyncSetUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        self.manager = MemoryManager(self.root)
        await self.manager.startup()
        await self.manager.save("kept")

    async def test_a_listing_skips_files_that_are_no_profile_and_says_so(self) -> None:
        (self.root / "a_list.json").write_text("[1, 2]", encoding="utf-8")
        (self.root / "broken.json").write_text("{not json", encoding="utf-8")

        with self.assertLogs("app.memory_manager", level="WARNING") as logs:
            profiles = await self.manager.list()

        self.assertEqual([profile["name"] for profile in profiles], ["kept"])
        logged = "\n".join(logs.output)
        self.assertIn("a_list.json", logged)
        self.assertIn("broken.json", logged)

    async def test_an_unreadable_profile_is_refused_by_name_without_its_content(self) -> None:
        (self.root / "broken.json").write_text('{"name": "broken", "notes": "SECRET-NOTE"', encoding="utf-8")
        calls = {
            "get": self.manager.get,
            "delete": self.manager.delete,
            "save": lambda name: self.manager.save(name, notes=["n"]),
        }

        for label, call in calls.items():
            with self.subTest(label):
                with self.assertRaisesRegex(ValueError, "memory profile 'broken' cannot be read") as caught:
                    await call("broken")
                self.assertNotIn("SECRET-NOTE", str(caught.exception))
        self.assertTrue((self.root / "broken.json").exists())

    async def test_a_cancelled_save_finishes_its_write_before_a_delete_runs(self) -> None:
        # Review of c4 (Codex 5): a save cancelled during its write let go of the lock while the write went on in its
        # thread, so a delete ran first and the write then brought the profile back.
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        write = memory_manager.atomic_write_text

        def paused(*args: object) -> None:
            started.set()
            release.wait(5)
            try:
                write(*args)
            finally:
                finished.set()

        with patch.object(memory_manager, "atomic_write_text", paused):
            saving = asyncio.ensure_future(self.manager.save("kept", notes=["more"]))
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            saving.cancel()
            deleting = asyncio.ensure_future(self.manager.delete("kept"))
            await asyncio.sleep(0.2)
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 5))
            with self.assertRaises(asyncio.CancelledError):
                await saving
            self.assertTrue(await deleting)

        self.assertFalse((self.root / "kept.json").exists())

    async def test_a_cancelled_delete_finishes_its_unlink_before_a_save_runs(self) -> None:
        # The other way round: the delete let go of the lock, a save read the profile and wrote it, and the unlink then
        # removed what the save had just written.
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        unlink = Path.unlink

        def paused(path: Path, *args: object, **kwargs: object) -> None:
            if path.name != "kept.json":
                return unlink(path, *args, **kwargs)
            started.set()
            release.wait(5)
            try:
                return unlink(path, *args, **kwargs)
            finally:
                finished.set()

        with patch.object(Path, "unlink", paused):
            deleting = asyncio.ensure_future(self.manager.delete("kept"))
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            deleting.cancel()
            saving = asyncio.ensure_future(self.manager.save("kept", notes=["after"]))
            await asyncio.sleep(0.2)
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 5))
            with self.assertRaises(asyncio.CancelledError):
                await deleting
            profile = await saving

        self.assertEqual(profile.notes, ["after"])
        self.assertTrue((self.root / "kept.json").exists())

    async def test_a_delete_waits_for_a_save_in_progress(self) -> None:
        async with self.manager._lock:
            deleting = asyncio.ensure_future(self.manager.delete("kept"))
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(deleting), 0.5)
            self.assertTrue((self.root / "kept.json").exists())

        self.assertTrue(await deleting)
        self.assertFalse((self.root / "kept.json").exists())
