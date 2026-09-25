""".env.example lists every setting the controller reads.

It is the file operators copy to configure a deployment, so a setting missing
from it is one they cannot discover without reading config.py.
OCR_SKIP_WHEN_TEXT_AVAILABLE and the six HARNESS_* settings had drifted out.

The controller image ships only app/ and tests/, so the test skips there.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


@unittest.skipUnless(ENV_EXAMPLE.is_file(), ".env.example is not in the controller image")
class EnvExampleTests(unittest.TestCase):
    def test_every_setting_is_listed(self) -> None:
        # Commented-out entries count: most settings have safe defaults and are
        # listed as `# NAME=default`.
        listed = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M))
        aliases = {field.alias for field in Settings.model_fields.values() if field.alias}
        self.assertTrue(aliases, "Settings declares no env aliases; the lookup has drifted")
        self.assertEqual(sorted(aliases - listed), [], "settings missing from .env.example")
