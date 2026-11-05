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

    def test_url_fields_that_can_carry_a_credential_stay_out_of_repr(self):
        # AUTH-25: the secret word scan misses these, and a URL's userinfo, path or query can be its credential.
        for alias, value in (
            ("APPROVAL_WEBHOOK_URL", f"https://hooks.example.com/services/{_SENTINEL}"),
            ("REDIS_URL", f"redis://:{_SENTINEL}@redis:6379/0"),
            ("CDP_CONNECT_URL", f"ws://127.0.0.1:9222/devtools/browser/{_SENTINEL}"),
            ("BROWSER_WS_ENDPOINT", f"ws://browser-node:9223/{_SENTINEL}"),
            ("DEFAULT_PROXY_SERVER", f"http://user:{_SENTINEL}@proxy.example:3128"),
        ):
            with self.subTest(alias=alias):
                settings = Settings(_env_file=None, **{alias: value})
                self.assertNotIn(_SENTINEL, repr(settings))
                self.assertNotIn(_SENTINEL, str(settings))
