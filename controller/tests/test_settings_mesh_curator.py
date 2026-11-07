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
