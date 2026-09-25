from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from app.auth_state import AuthStateManager


class FakeContext:
    async def storage_state(self, path: str) -> None:
        Path(path).write_text(
            json.dumps({"cookies": [{"name": "sid", "value": "abc123"}], "origins": []}),
            encoding="utf-8",
        )


class AuthStateManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_encrypts_and_prepare_restores_plain_json(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manager = AuthStateManager(
                encryption_key=Fernet.generate_key().decode("utf-8"),
                require_encryption=True,
                max_age_hours=72,
            )

            info = await manager.write_storage_state(FakeContext(), root / "session.json")

            self.assertTrue(info["encrypted"])
            self.assertTrue(info["path"].endswith("session.json.enc"))
            stored_path = Path(info["path"])
            payload = json.loads(stored_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["format"], "fernet-json")
            self.assertIn("ciphertext", payload)

            prepared = manager.prepare_for_context(stored_path)
            try:
                restored = json.loads(prepared.path.read_text(encoding="utf-8"))
                self.assertEqual(restored["cookies"][0]["name"], "sid")
                self.assertTrue(prepared.cleanup_path is not None)
            finally:
                prepared.cleanup()

            self.assertFalse(prepared.path.exists())

    async def test_inspect_marks_stale_and_prepare_rejects_old_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manager = AuthStateManager(
                encryption_key=None,
                require_encryption=False,
                max_age_hours=0.001,
            )
            state_path = root / "old-state.json"
            state_path.write_text("{}", encoding="utf-8")

            old_timestamp = state_path.stat().st_mtime - 3600
            import os

            os.utime(state_path, (old_timestamp, old_timestamp))

            info = manager.inspect(state_path)
            self.assertTrue(info["exists"])
            self.assertTrue(info["stale"])

            with self.assertRaises(PermissionError):
                manager.prepare_for_context(state_path)

    async def test_plain_output_path_strips_enc_suffix_without_encryption(self) -> None:
        manager = AuthStateManager(
            encryption_key=None,
            require_encryption=False,
            max_age_hours=72,
        )

        self.assertEqual(
            manager.output_path(Path("/tmp/demo.json.enc")),
            Path("/tmp/demo.json"),
        )

    async def test_prepare_for_context_accepts_a_max_age_override(self) -> None:
        # The unattended "remember me" / cron path uses a much larger limit
        # than the interactive default so it never silently fails just
        # because nobody happened to open the browser recently.
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manager = AuthStateManager(encryption_key=None, require_encryption=False, max_age_hours=1)
            state_path = root / "state.json"
            state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
            import os

            old_timestamp = state_path.stat().st_mtime - 5 * 3600  # 5h old
            os.utime(state_path, (old_timestamp, old_timestamp))

            with self.assertRaises(PermissionError):
                manager.prepare_for_context(state_path)  # default 1h limit: stale

            prepared = manager.prepare_for_context(state_path, max_age_hours=24)
            try:
                self.assertTrue(prepared.path.exists())
            finally:
                prepared.cleanup()

    async def test_write_storage_state_rotates_previous_file_into_history(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manager = AuthStateManager(encryption_key=None, require_encryption=False, max_age_hours=72)
            destination = root / "state.json"

            await manager.write_storage_state(FakeContext(), destination)
            history_before = list(root.glob("state.json.*"))
            self.assertEqual(history_before, [])  # nothing to rotate on the first write

            await manager.write_storage_state(FakeContext(), destination)
            history_after = list(root.glob("state.json.*"))
            self.assertEqual(len(history_after), 1)
            # The rotated copy holds what was live *before* this write, and the
            # live file is never mistaken for a history file.
            rotated = json.loads(history_after[0].read_text(encoding="utf-8"))
            self.assertEqual(rotated["cookies"][0]["name"], "sid")
            self.assertTrue(destination.exists())

    async def test_write_storage_state_prunes_history_beyond_the_keep_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manager = AuthStateManager(
                encryption_key=None, require_encryption=False, max_age_hours=72, history_keep=2
            )
            destination = root / "state.json"

            for _ in range(5):
                await manager.write_storage_state(FakeContext(), destination)

            # Every write prunes down to the keep limit immediately, so the
            # count is deterministic regardless of same-second collisions.
            history_files = [p for p in root.glob("state.json.*")]
            self.assertEqual(len(history_files), 2)

    async def test_history_files_are_never_mistaken_for_the_live_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manager = AuthStateManager(encryption_key=None, require_encryption=False, max_age_hours=72)
            destination = root / "state.json"

            await manager.write_storage_state(FakeContext(), destination)
            await manager.write_storage_state(FakeContext(), destination)

            # inspect() on the live path must still see exactly the live file.
            info = manager.inspect(destination)
            self.assertTrue(info["exists"])
            self.assertEqual(info["path"], str(destination))
