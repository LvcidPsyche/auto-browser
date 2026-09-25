"""The README's production settings block starts a production controller.

The block is introduced as what to "set at least" for a real deployment, but it
left out SHARE_TOKEN_SECRET and CONTROLLER_ALLOWED_HOSTS, both of which
APP_ENV=production requires, so a deployment that followed it refused to start.
This fills the block's placeholders with valid values and runs the startup
policy over it.

The controller image ships only app/ and tests/, so the test skips there.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import Settings
from app.runtime_policy import validate_runtime_policy

README = Path(__file__).resolve().parents[2] / "README.md"

PLACEHOLDERS = {
    "<strong-random-secret>": "s" * 48,
    "<44-char-fernet-key>": Fernet.generate_key().decode(),
    "<controller hostname>": "browser.internal.example",
    "<sites the browser may visit>": "app.internal.example",
}


def _production_block() -> dict[str, str]:
    text = README.read_text(encoding="utf-8")
    match = re.search(r"```bash\n(APP_ENV=production\n.*?)```", text, re.S)
    if match is None:
        raise AssertionError("README has no ```bash block starting with APP_ENV=production")
    values = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition("=")
        values[key.strip()] = PLACEHOLDERS.get(value.strip(), value.strip())
    return values


@unittest.skipUnless(README.is_file(), "README.md is not in the controller image")
class ReadmeProductionSettingsTests(unittest.TestCase):
    def test_the_documented_settings_pass_the_production_startup_policy(self) -> None:
        values = _production_block()
        unfilled = [key for key, value in values.items() if value.startswith("<")]
        self.assertEqual(unfilled, [], "add the new placeholder to PLACEHOLDERS")

        settings = Settings(_env_file=None, **values)
        report = validate_runtime_policy(settings)

        self.assertTrue(settings.is_production)
        self.assertEqual(report.errors, [])
