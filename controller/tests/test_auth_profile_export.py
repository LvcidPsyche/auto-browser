"""An exported auth profile carries only the encrypted copy of its state.

Saving with an encryption key writes state.json.enc, but a plaintext state.json
saved before the key was set stays in the profile directory. Export packed both,
so the archive (the thing that leaves the box) held every cookie in the clear
next to the encrypted copy it was supposed to be replaced by.
"""

from __future__ import annotations

import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.browser.services.auth_profiles import BrowserAuthProfileService


class _Audit:
    async def append(self, **_kwargs) -> None:
        return None


class ExportPlaintextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.service = BrowserAuthProfileService(
            SimpleNamespace(
                settings=SimpleNamespace(auth_root=str(self.root), auth_state_encryption_key="set"),
                audit=_Audit(),
                witness=None,
            )
        )

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def _profile(self, name: str, **files: str) -> Path:
        directory = self.root / "profiles" / name
        directory.mkdir(parents=True)
        for filename, text in files.items():
            (directory / filename.replace("__", ".")).write_text(text, encoding="utf-8")
        return directory

    async def _exported_names(self, name: str) -> list[str]:
        exported = await self.service.export(name)
        with tarfile.open(exported["archive_path"]) as archive:
            return sorted(member.name for member in archive.getmembers() if member.isfile())

    async def test_plaintext_state_is_left_out_when_an_encrypted_copy_exists(self) -> None:
        self._profile(
            "alice-login",
            profile__json='{"profile_name": "alice-login"}',
            state__json='{"cookies": [{"value": "PLAINTEXT-SECRET"}]}',
            state__json__enc='{"ciphertext": "x"}',
        )

        names = await self._exported_names("alice-login")

        self.assertEqual(names, ["alice-login/profile.json", "alice-login/state.json.enc"])

    async def test_plaintext_state_is_still_exported_when_it_is_all_there_is(self) -> None:
        """Deployments without an encryption key keep exporting their state."""
        self._profile("legacy", profile__json="{}", state__json='{"cookies": []}')

        names = await self._exported_names("legacy")

        self.assertEqual(names, ["legacy/profile.json", "legacy/state.json"])


if __name__ == "__main__":
    unittest.main()
