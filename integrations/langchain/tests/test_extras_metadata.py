"""The `crewai` extra must bring in what the CrewAI example imports.

examples/crewai-integration.md builds AutoBrowserTool, a langchain-core BaseTool. Recent crewai releases no longer
depend on langchain, so `pip install auto-browser-langchain[crewai]` alone left the tool unable to import.
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
LANGCHAIN_EXTRA = "auto-browser-langchain[langchain]"


@unittest.skipUnless(PYPROJECT.is_file(), "pyproject.toml is not beside the tests")
class ExtrasMetadataTests(unittest.TestCase):
    def test_the_crewai_extra_brings_in_langchain_core(self) -> None:
        extras = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["optional-dependencies"]

        self.assertTrue(any(requirement.startswith("langchain-core") for requirement in extras["langchain"]))
        self.assertIn(LANGCHAIN_EXTRA, extras["crewai"])


if __name__ == "__main__":
    unittest.main()
