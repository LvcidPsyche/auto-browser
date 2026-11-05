"""Reading an auth profile that does not exist must not leave a directory behind."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.browser_manager import BrowserManager
from tests.test_browser_manager_create_session import _settings


class AuthProfileReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_getting_a_missing_profile_creates_no_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = BrowserManager(_settings(Path(tmp)))
            profiles_root = Path(manager.settings.auth_root) / "profiles"

            for name in ("ghost-one", "ghost-two"):
                with self.assertRaises(KeyError):
                    await manager.auth_profiles.get(name)

            self.assertEqual(sorted(p.name for p in profiles_root.glob("*")) if profiles_root.exists() else [], [])
