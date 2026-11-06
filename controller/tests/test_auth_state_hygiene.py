"""STORE-21 and STORE-22: no plaintext left beside an encrypted save, and a typed error for an unreadable state."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth_state import AUTH_STATE_UNREADABLE, AuthStateDecryptError, AuthStateManager
from app.routes.sessions import create_sessions_router


class _Context:
    async def storage_state(self, path: str | None = None) -> dict:
        state = {"cookies": [{"name": "sid", "value": "abc123"}], "origins": []}
        if path is not None:
            Path(path).write_text(json.dumps(state), encoding="utf-8")
        return state


def _manager(key: str | None, *, require: bool = False) -> AuthStateManager:
    return AuthStateManager(encryption_key=key, require_encryption=require, max_age_hours=72)


class PlaintextSiblingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    async def test_an_encrypted_save_removes_the_plaintext_state_it_replaces(self) -> None:
        plain = self.root / "session.json"
        plain.write_text(json.dumps({"cookies": [{"name": "old", "value": "secret"}], "origins": []}), encoding="utf-8")

        info = await _manager(Fernet.generate_key().decode()).write_storage_state(_Context(), plain)

        self.assertTrue(info["path"].endswith("session.json.enc"))
        self.assertFalse(plain.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["session.json.enc"])

    async def test_w4_16_a_plaintext_sibling_that_cannot_be_removed_only_warns(self) -> None:
        # The removal was unguarded: a sibling that could not go (a directory, a Windows lock) failed a save that had
        # already written the encrypted state.
        target = self.root / "session.json.enc"
        (self.root / "session.json").mkdir()

        with self.assertLogs("app.auth_state", "WARNING") as logged:
            info = await _manager(Fernet.generate_key().decode()).write_storage_state(_Context(), target)

        self.assertTrue(info["path"].endswith("session.json.enc"))
        self.assertTrue(target.is_file())
        self.assertIn(str(self.root / "session.json"), "\n".join(logged.output))

    async def test_an_unencrypted_save_leaves_its_own_file(self) -> None:
        target = self.root / "session.json"
        await _manager(None).write_storage_state(_Context(), target)
        self.assertTrue(target.exists())

    async def test_a_failed_encrypted_save_keeps_the_plaintext_it_could_not_replace(self) -> None:
        plain = self.root / "session.json"
        plain.write_text("{}", encoding="utf-8")

        class _Broken:
            async def storage_state(self, path: str | None = None) -> dict:
                raise RuntimeError("browser gone")

        with self.assertRaises(RuntimeError):
            await _manager(Fernet.generate_key().decode()).write_storage_state(_Broken(), plain)
        self.assertTrue(plain.exists())

    def test_loading_plaintext_while_a_key_is_configured_warns(self) -> None:
        plain = self.root / "session.json"
        plain.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        manager = _manager(Fernet.generate_key().decode())
        with self.assertLogs("app.auth_state", level="WARNING") as captured:
            prepared = manager.prepare_for_context(plain)
        self.assertEqual(prepared.storage_state, str(plain))
        self.assertTrue(any("not encrypted" in line for line in captured.output))

    def test_loading_plaintext_with_no_key_does_not_warn(self) -> None:
        plain = self.root / "session.json"
        plain.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        with self.assertNoLogs("app.auth_state", level="WARNING"):
            _manager(None).prepare_for_context(plain)


class DecryptErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    async def _encrypted_file(self, key: str) -> Path:
        info = await _manager(key).write_storage_state(_Context(), self.root / "s.json")
        return Path(info["path"])

    async def test_the_wrong_key_is_a_typed_error(self) -> None:
        path = await self._encrypted_file(Fernet.generate_key().decode())
        with self.assertRaises(AuthStateDecryptError) as caught:
            _manager(Fernet.generate_key().decode()).prepare_for_context(path)
        self.assertEqual(str(caught.exception), AUTH_STATE_UNREADABLE)

    async def test_no_key_at_all_is_the_same_typed_error(self) -> None:
        path = await self._encrypted_file(Fernet.generate_key().decode())
        with self.assertRaises(AuthStateDecryptError):
            _manager(None).prepare_for_context(path)

    async def test_a_corrupt_envelope_is_the_same_typed_error(self) -> None:
        key = Fernet.generate_key().decode()
        path = await self._encrypted_file(key)
        for body in (
            '{"format": "fernet-json", "ciphertext": "not-fernet"}',
            '{"format": "fernet-json"}',
            '{"format": "fernet-json", "ciphertext": "' + "A" * 8 + '"}',
        ):
            with self.subTest(body=body[:40]):
                path.write_text(body, encoding="utf-8")
                with self.assertRaises(AuthStateDecryptError):
                    _manager(key).prepare_for_context(path)

    async def test_valid_ciphertext_that_is_not_json_is_the_same_typed_error(self) -> None:
        key = Fernet.generate_key().decode()
        ciphertext = Fernet(key.encode()).encrypt(b"not json").decode()
        path = self.root / "bad.json.enc"
        path.write_text(json.dumps({"version": 1, "format": "fernet-json", "ciphertext": ciphertext}), encoding="utf-8")
        with self.assertRaises(AuthStateDecryptError):
            _manager(key).prepare_for_context(path)

    def test_the_error_is_still_a_runtime_error_for_callers_that_map_those(self) -> None:
        self.assertTrue(issubclass(AuthStateDecryptError, RuntimeError))


class SessionRouteTests(unittest.TestCase):
    def test_create_session_answers_the_fixed_text(self) -> None:
        async def create_session(**kwargs):
            raise AuthStateDecryptError(AUTH_STATE_UNREADABLE)

        app = FastAPI()
        app.include_router(create_sessions_router(manager=SimpleNamespace(create_session=create_session)))
        response = TestClient(app, raise_server_exceptions=False).post("/sessions", json={"name": "n"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": AUTH_STATE_UNREADABLE})


if __name__ == "__main__":
    unittest.main()
