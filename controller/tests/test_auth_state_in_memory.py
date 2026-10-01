"""Decrypted auth state never touches disk.

Opening a session from an encrypted auth profile decrypted the state into a temp
file beside the encrypted one — inside the profile directory — and removed it
only when create_session returned, after the start URL had loaded. An export
taken in that window packed the plaintext cookies into the archive while the
audit event recorded `encrypted_at_rest: true`, and a process that died in the
window left them there for every later export. Saving encrypted state wrote the
plaintext to a temp file first as well.
"""

from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

from app.auth_state import AuthStateManager
from app.browser.services.auth_profiles import BrowserAuthProfileService

STATE = {"cookies": [{"name": "session", "value": "SECRET-COOKIE-VALUE", "domain": "example.com", "path": "/"}], "origins": []}


class MemoryOnlyContext:
    """Playwright's storage_state returns the state, writing it only when given a path."""

    def __init__(self) -> None:
        self.paths: list[str | None] = []

    async def storage_state(self, path: str | None = None) -> dict:
        self.paths.append(path)
        if path is not None:
            Path(path).write_text(json.dumps(STATE), encoding="utf-8")
        return STATE


class _Audit:
    async def append(self, **_kwargs) -> None:
        return None


class InMemoryAuthStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.auth = AuthStateManager(encryption_key=Fernet.generate_key().decode(), require_encryption=True, max_age_hours=72)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    def _files_holding_the_cookie(self) -> list[str]:
        return [str(p) for p in self.root.rglob("*") if p.is_file() and b"SECRET-COOKIE-VALUE" in p.read_bytes()]

    async def test_saving_encrypted_state_writes_no_plaintext(self) -> None:
        context = MemoryOnlyContext()
        info = await self.auth.write_storage_state(context, self.root / "profile" / "state.json")

        self.assertTrue(info["encrypted"])
        self.assertEqual(context.paths, [None], "the state must be taken in memory, not through a file")
        self.assertEqual(self._files_holding_the_cookie(), [])

    async def test_preparing_encrypted_state_returns_it_without_writing_a_file(self) -> None:
        stored = Path((await self.auth.write_storage_state(MemoryOnlyContext(), self.root / "state.json"))["path"])
        before = sorted(p.name for p in self.root.iterdir())

        prepared = self.auth.prepare_for_context(stored)

        self.assertEqual(prepared.storage_state, STATE)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)
        self.assertEqual(self._files_holding_the_cookie(), [])

    async def test_an_export_taken_while_a_session_opens_holds_no_plaintext(self) -> None:
        profiles = BrowserAuthProfileService(
            SimpleNamespace(
                settings=SimpleNamespace(auth_root=str(self.root / "auth"), auth_state_encryption_key="set"),
                audit=_Audit(),
                witness=None,
                auth_state=self.auth,
            )
        )
        await self.auth.write_storage_state(MemoryOnlyContext(), profiles.state_base_path("work", create=True))
        source = profiles.resolve_state_path("work", must_exist=True)
        # A leftover from an older release that died mid-create, and a torn save.
        (source.parent / "auth-state-abc123.json").write_text(json.dumps(STATE), encoding="utf-8")
        (source.parent / ".state.json.enc.xyz.tmp.json").write_text(json.dumps(STATE), encoding="utf-8")

        self.auth.prepare_for_context(source)  # what opening a session does
        exported = await profiles.export("work")

        with tarfile.open(exported["archive_path"]) as archive:
            leaked = [m.name for m in archive.getmembers() if m.isfile() and b"SECRET-COOKIE-VALUE" in archive.extractfile(m).read()]
            names = [m.name for m in archive.getmembers()]
        self.assertEqual(leaked, [])
        self.assertTrue(any(name.endswith("state.json.enc") for name in names), names)


if __name__ == "__main__":
    unittest.main()
