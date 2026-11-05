from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.config import Settings


def _settings_from_env(**env: str) -> Settings:
    with patch.dict(os.environ, env):
        return Settings(_env_file=None)


class SessionIsolationModeTests(unittest.TestCase):
    def test_surrounding_space_and_case_are_forgiven(self):
        for raw in (" docker_ephemeral ", "Docker_Ephemeral", "DOCKER_EPHEMERAL\n"):
            settings = _settings_from_env(SESSION_ISOLATION_MODE=raw)
            self.assertEqual(settings.session_isolation_mode, "docker_ephemeral", raw)
        settings = _settings_from_env(SESSION_ISOLATION_MODE=" Shared_Browser_Node")
        self.assertEqual(settings.session_isolation_mode, "shared_browser_node")

    def test_a_misspelled_mode_stops_startup_and_names_the_valid_ones(self):
        for raw in ("docker-ephemeral", "dockerephemeral", "docker", "shared"):
            with self.assertRaises(ValidationError) as ctx:
                _settings_from_env(SESSION_ISOLATION_MODE=raw)
            message = str(ctx.exception)
            self.assertIn("docker_ephemeral", message, raw)
            self.assertIn("shared_browser_node", message, raw)


if __name__ == "__main__":
    unittest.main()
