from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import Settings

_PROVIDER_KEY_FIELDS = (
    "openai_api_key",
    "anthropic_api_key",
    "gemini_api_key",
    "openrouter_api_key",
    "xai_api_key",
    "deepseek_api_key",
    "minimax_api_key",
    "openai_compatible_api_key",
)


def _settings_from_env(**env: str) -> Settings:
    # Through the environment, as a deployment sets it: field validators do not run on setattr.
    with patch.dict(os.environ, env):
        return Settings(_env_file=None)


class ProviderApiKeyWhitespaceTests(unittest.TestCase):
    def test_whitespace_around_a_provider_key_is_stripped(self):
        for field in _PROVIDER_KEY_FIELDS:
            alias = Settings.model_fields[field].alias
            for raw in (" sk-test", "sk-test ", "\tsk-test\n"):
                with self.subTest(field=field, raw=raw):
                    self.assertEqual(getattr(_settings_from_env(**{alias: raw}), field), "sk-test")

    def test_a_whitespace_only_provider_key_is_unset(self):
        for field in _PROVIDER_KEY_FIELDS:
            alias = Settings.model_fields[field].alias
            with self.subTest(field=field):
                self.assertIsNone(getattr(_settings_from_env(**{alias: "  \n"}), field))

    def test_a_clean_key_is_unchanged(self):
        self.assertEqual(_settings_from_env(OPENROUTER_API_KEY="sk-or-v1-abc").openrouter_api_key, "sk-or-v1-abc")


_SECRET_FIELDS = ("api_bearer_token", "witness_remote_api_key")


class SecretWhitespaceTests(unittest.TestCase):
    """The shared bearer token and the witness key are stripped like the provider keys."""

    def test_whitespace_around_a_secret_is_stripped(self):
        for field in _SECRET_FIELDS:
            alias = Settings.model_fields[field].alias
            for raw in (" tok", "tok ", "\ttok\n"):
                with self.subTest(field=field, raw=raw):
                    self.assertEqual(getattr(_settings_from_env(**{alias: raw}), field), "tok")

    def test_a_whitespace_only_secret_is_unset(self):
        for field in _SECRET_FIELDS:
            alias = Settings.model_fields[field].alias
            with self.subTest(field=field):
                self.assertIsNone(getattr(_settings_from_env(**{alias: "  \n"}), field))


if __name__ == "__main__":
    unittest.main()
