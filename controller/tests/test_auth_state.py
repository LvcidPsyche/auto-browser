from __future__ import annotations

import asyncio
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

    async def test_failed_encryption_leaves_no_plaintext_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manager = AuthStateManager(
                encryption_key=Fernet.generate_key().decode("utf-8"),
                require_encryption=True,
                max_age_hours=72,
            )
            manager._fernet = None  # encryption fails after the plaintext is written
            (root / "profile").mkdir()

            with self.assertRaises(RuntimeError):
                await manager.write_storage_state(FakeContext(), root / "profile" / "state.json")

            self.assertEqual(list((root / "profile").iterdir()), [])

    async def test_concurrent_saves_to_one_path_all_succeed(self) -> None:
        class SlowContext:
            async def storage_state(self, path: str) -> None:
                Path(path).write_text("{", encoding="utf-8")
                await asyncio.sleep(0.01)
                Path(path).write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manager = AuthStateManager(
                encryption_key=Fernet.generate_key().decode("utf-8"),
                require_encryption=True,
                max_age_hours=72,
            )

            results = await asyncio.gather(
                *(manager.write_storage_state(SlowContext(), root / "state.json") for _ in range(4)),
                return_exceptions=True,
            )

            self.assertEqual([r for r in results if isinstance(r, Exception)], [])
            self.assertEqual([p.name for p in root.iterdir()], ["state.json.enc"])
            prepared = manager.prepare_for_context(root / "state.json.enc")
            try:
                self.assertEqual(json.loads(prepared.path.read_text(encoding="utf-8"))["cookies"], [])
            finally:
                prepared.cleanup()

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
