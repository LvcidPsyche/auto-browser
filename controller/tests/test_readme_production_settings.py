"""The production settings blocks of the README and docs/deployment.md start a production controller.

The README's block is introduced as what to "set at least" for a real deployment, but it
left out SHARE_TOKEN_SECRET and CONTROLLER_ALLOWED_HOSTS, both of which
APP_ENV=production requires, so a deployment that followed it refused to start.
docs/deployment.md's "At minimum" block left out SHARE_TOKEN_SECRET the same way.
This fills each block's placeholders with valid values and runs the startup
policy over it, with nothing from the environment: tests/__init__.py gives the
whole suite CONTROLLER_ALLOWED_HOSTS, and a block that left it out was given it,
and passed.

The controller image ships only app/ and tests/, so the test skips there.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.config import Settings
from app.runtime_policy import validate_runtime_policy

REPO = Path(__file__).resolve().parents[2]
# Each document, and the fenced block in it that opens with APP_ENV=production.
BLOCKS = (
    (REPO / "README.md", r"```bash\n(APP_ENV=production\n.*?)```"),
    (REPO / "docs" / "deployment.md", r"```env\n(APP_ENV=production\n.*?)```"),
)
PLACEHOLDERS = {
    "<strong-random-secret>": "s" * 48,
    "<44-char-fernet-key>": Fernet.generate_key().decode(),
    "<controller hostname>": "browser.internal.example",
    "<controller-ingress-hosts>": "browser.internal.example",
    "<sites the browser may visit>": "app.internal.example",
}


def _production_block(path: Path, pattern: str) -> dict[str, str]:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.S)
    if match is None:
        raise AssertionError(f"{path.name} has no block starting with APP_ENV=production")
    values = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition("=")
        values[key.strip()] = PLACEHOLDERS.get(value.strip(), value.strip())
    return values


@unittest.skipUnless(all(path.is_file() for path, _ in BLOCKS), "the docs are not in the controller image")
class ProductionSettingsBlocksTests(unittest.TestCase):
    def test_each_documented_block_passes_the_production_startup_policy(self) -> None:
        for path, pattern in BLOCKS:
            with self.subTest(path.relative_to(REPO).as_posix()):
                values = _production_block(path, pattern)
                unfilled = [key for key, value in values.items() if value.startswith("<")]
                self.assertEqual(unfilled, [], "add the new placeholder to PLACEHOLDERS")

                with patch.dict(os.environ, clear=True):
                    settings = Settings(_env_file=None, **values)
                report = validate_runtime_policy(settings)

                self.assertTrue(settings.is_production)
                self.assertEqual(report.errors, [])
