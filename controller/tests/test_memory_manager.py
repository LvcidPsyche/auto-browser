from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.memory_manager import MemoryManager, MemoryProfile, extract_hostname


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

    async def test_get_by_url(self) -> None:
        await self.mem.startup()
        await self.mem.save(
            "github.com",
            goal_summary="GitHub workflows",
            discovered_selectors={"login_btn": "#login"},
        )
        # Should find profile by URL hostname
        profile = await self.mem.get_by_url("https://github.com/user/repo")
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.name, "github.com")
        self.assertEqual(profile.goal_summary, "GitHub workflows")

    async def test_get_by_url_not_found(self) -> None:
        await self.mem.startup()
        # No profile for this hostname
        profile = await self.mem.get_by_url("https://example.com/page")
        self.assertIsNone(profile)

    async def test_get_by_url_invalid(self) -> None:
        await self.mem.startup()
        # Invalid URLs should return None, not raise
        self.assertIsNone(await self.mem.get_by_url("not-a-url"))
        self.assertIsNone(await self.mem.get_by_url(""))


class ExtractHostnameTests(unittest.TestCase):
    def test_valid_urls(self) -> None:
        self.assertEqual(extract_hostname("https://github.com/user"), "github.com")
        self.assertEqual(extract_hostname("http://example.org:8080/path"), "example.org")
        self.assertEqual(extract_hostname("https://sub.domain.co.uk"), "sub.domain.co.uk")

    def test_case_normalization(self) -> None:
        self.assertEqual(extract_hostname("https://GitHub.COM/"), "github.com")

    def test_path_traversal_rejected(self) -> None:
        # These should be rejected to prevent path traversal attacks
        self.assertIsNone(extract_hostname("https://.."))
        self.assertIsNone(extract_hostname("https://.hidden"))
        self.assertIsNone(extract_hostname("https://trailing."))

    def test_invalid_urls(self) -> None:
        self.assertIsNone(extract_hostname("not-a-url"))
        self.assertIsNone(extract_hostname(""))
        self.assertIsNone(extract_hostname("file:///etc/passwd"))


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
