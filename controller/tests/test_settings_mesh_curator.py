"""The mesh and curator knobs are Settings fields, read from the controller's settings (AUTH-14).

They used to be read from os.environ in startup/extensions.py and curator_llm.py, so a controller started with
injected settings (and the settings validators) never saw them.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.config import Settings
from app.curator_llm import CuratorLLMAdapter, build_curator_adapter
from app.startup.extensions import _init_mesh


class MeshAndCuratorSettingsTests(unittest.TestCase):
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(_env_file=None)
        self.assertFalse(settings.mesh_enabled)
        self.assertEqual(settings.mesh_identity_dir, "/data/mesh/identity")
        self.assertEqual(settings.mesh_peers_path, "/data/mesh/peers.json")
        self.assertEqual(settings.mesh_timestamp_window, 30.0)
        self.assertEqual(settings.curator_provider, "claude")
        self.assertIsNone(settings.curator_model)

    def test_read_from_the_environment_names_the_docs_use(self):
        env = {
            "MESH_ENABLED": "true",
            "MESH_IDENTITY_DIR": "/x/id",
            "MESH_PEERS_PATH": "/x/peers.json",
            "MESH_TIMESTAMP_WINDOW": "45",
            "CURATOR_PROVIDER": "openai",
            "CURATOR_MODEL": "gpt-test",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
        self.assertTrue(settings.mesh_enabled)
        self.assertEqual(settings.mesh_identity_dir, "/x/id")
        self.assertEqual(settings.mesh_peers_path, "/x/peers.json")
        self.assertEqual(settings.mesh_timestamp_window, 45.0)
        self.assertEqual(settings.curator_provider, "openai")
        self.assertEqual(settings.curator_model, "gpt-test")


class MeshTimestampWindowBoundsTests(unittest.TestCase):
    """R5C-26: MESH_TIMESTAMP_WINDOW had no bounds.

    A nonce is kept for up to two windows, so at the default rate limit one address could fill the 10,000-nonce replay
    cache from a window of about 2,500 s, and a wide window keeps a captured envelope replayable longer to a restarted
    controller; at or below 0 the window refuses every envelope. The ceiling is 250 s, not 600 (review of c2, GLM 4):
    at 600 a peer sending at the default rate limit held more nonces than its 1,000 share of the cache.
    """

    def _settings(self, raw: str) -> Settings:
        with patch.dict(os.environ, {"MESH_TIMESTAMP_WINDOW": raw}, clear=True):
            return Settings(_env_file=None)

    def test_r5c_26_a_window_above_250_seconds_or_at_or_below_0_stops_startup_and_names_its_limits(self):
        # The mesh is off here: a value out of bounds stops startup either way, as an unparseable one does.
        for raw in ("250.5", "251", "600", "3600", "0", "-30"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValidationError) as ctx:
                    self._settings(raw)
                message = str(ctx.exception)
                self.assertIn("MESH_TIMESTAMP_WINDOW must be more than 0 and at most 250 seconds", message)

    def test_r5c_26_a_window_inside_its_limits_is_kept_the_ceiling_included(self):
        for raw, expected in (("0.5", 0.5), ("30", 30.0), ("250", 250.0)):
            with self.subTest(raw=raw):
                self.assertEqual(self._settings(raw).mesh_timestamp_window, expected)


class InitMeshReadsSettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _app(self, **settings_kwargs):
        return SimpleNamespace(state=SimpleNamespace(settings=Settings(_env_file=None, **settings_kwargs)))

    def test_mesh_is_built_from_the_settings_values(self):
        app = self._app(
            MESH_ENABLED=True,
            MESH_IDENTITY_DIR=str(self.root / "id"),
            MESH_PEERS_PATH=str(self.root / "peers.json"),
            MESH_TIMESTAMP_WINDOW=45,
        )
        with patch.dict(os.environ, {"MESH_IDENTITY_DIR": str(self.root / "from-env")}):
            _init_mesh(app)
        self.assertIsNotNone(app.state.mesh_identity)
        self.assertTrue((self.root / "id" / "node_private.pem").exists())
        self.assertFalse((self.root / "from-env").exists())
        self.assertEqual(app.state.delegation_manager._timestamp_window, 45.0)

    def test_the_environment_alone_no_longer_enables_mesh(self):
        app = self._app()
        with patch.dict(os.environ, {"MESH_ENABLED": "true"}):
            _init_mesh(app)
        self.assertIsNone(app.state.mesh_identity)


class CuratorReadsSettingsTests(unittest.TestCase):
    def test_provider_and_model_come_from_settings(self):
        settings = Settings(_env_file=None, CURATOR_PROVIDER="gemini", CURATOR_MODEL="gem-test")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k", "CURATOR_PROVIDER": "openai"}, clear=True):
            adapter = build_curator_adapter(settings)
        self.assertEqual((adapter.provider, adapter.model), ("gemini", "gem-test"))

    def test_without_settings_the_environment_is_still_read(self):
        with patch.dict(os.environ, {"CURATOR_PROVIDER": "openai", "OPENAI_API_KEY": "k"}, clear=True):
            adapter = CuratorLLMAdapter.from_env()
        self.assertEqual(adapter.provider, "openai")


if __name__ == "__main__":
    unittest.main()
