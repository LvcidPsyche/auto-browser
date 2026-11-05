from __future__ import annotations

import os
import re
import unittest
from unittest.mock import patch

from app.config import Settings

_SECRET_WORD = re.compile(r"KEY|TOKEN|SECRET|PASSWORD")
# Names that contain a secret word but hold a path, a lifetime or a mode, not a secret.
_NOT_SECRET_SUFFIX = ("_PATH", "_FILE", "_MINUTES", "_SECONDS", "_MODE", "_HEADER", "_CHECKING")
_SENTINEL = "sentinel-secret-value-7f3a"


def _secret_aliases() -> dict[str, str]:
    found: dict[str, str] = {}
    for name, field in Settings.model_fields.items():
        alias = field.alias
        if not isinstance(alias, str) or not _SECRET_WORD.search(alias):
            continue
        if alias.endswith(_NOT_SECRET_SUFFIX) or field.annotation not in (str, str | None):
            continue
        found[name] = alias
    return found


class SecretsStayOutOfReprTests(unittest.TestCase):
    def test_the_secret_field_scan_finds_the_known_secrets(self):
        names = set(_secret_aliases())
        for expected in (
            "auth_state_encryption_key",
            "witness_remote_api_key",
            "openai_api_key",
            "approval_webhook_secret",
            "default_proxy_password",
            "share_token_secret",
            "vnc_password",
        ):
            self.assertIn(expected, names)

    def test_no_secret_value_appears_in_repr(self):
        for name, alias in _secret_aliases().items():
            with self.subTest(field=name):
                with patch.dict(os.environ, {alias: _SENTINEL}):
                    settings = Settings(_env_file=None)
                self.assertNotIn(_SENTINEL, repr(settings))
                self.assertNotIn(_SENTINEL, str(settings))
