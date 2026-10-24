"""The lint gate selects Ruff's `S` rules, and nothing overrides the config's selection.

A `--select` on the command line replaces the config's `select`, so widening the
config alone does nothing while `make lint` or `release_audit.py` still pass
`--select E9,F,I`. Both configs and both callers are checked together.

The controller image ships only app/ and tests/, so the test skips there.
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_RUFF = REPO_ROOT / "ruff.toml"
CONTROLLER_PYPROJECT = REPO_ROOT / "controller" / "pyproject.toml"
MAKEFILE = REPO_ROOT / "Makefile"
RELEASE_AUDIT = REPO_ROOT / "scripts" / "release_audit.py"


@unittest.skipUnless(ROOT_RUFF.is_file(), "repo root is not in the controller image")
class LintConfigTests(unittest.TestCase):
    def test_both_configs_select_the_security_rules(self) -> None:
        root = tomllib.loads(ROOT_RUFF.read_text(encoding="utf-8"))["lint"]["select"]
        controller = tomllib.loads(CONTROLLER_PYPROJECT.read_text(encoding="utf-8"))["tool"]["ruff"]["lint"]["select"]
        self.assertIn("S", root)
        self.assertIn("S", controller)

    def test_no_caller_overrides_the_configured_selection(self) -> None:
        lint_recipe = MAKEFILE.read_text(encoding="utf-8").split("\nlint:", 1)[1].split("\n\n", 1)[0]
        self.assertIn("ruff check .", lint_recipe)
        self.assertNotIn("--select", lint_recipe)
        self.assertNotIn("--select", RELEASE_AUDIT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
