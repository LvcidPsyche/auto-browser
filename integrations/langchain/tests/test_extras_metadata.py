"""The `crewai` extra must bring in what the CrewAI example imports.

examples/crewai-integration.md builds AutoBrowserTool, a langchain-core BaseTool. Recent crewai releases no longer
depend on langchain, so `pip install auto-browser-langchain[crewai]` alone left the tool unable to import.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def resolved_requirements(project: dict, extra: str) -> list[str]:
    """An extra's requirements, with a `<project name>[x]` self-reference replaced by extra x's (one level)."""
    name = project["name"]
    extras = project["optional-dependencies"]
    resolved: list[str] = []
    for requirement in extras[extra]:
        match = re.fullmatch(rf"{re.escape(name)}\[([^\]]+)\]", requirement.strip())
        if match:
            resolved.extend(extras[match.group(1)])
        else:
            resolved.append(requirement)
    return resolved


@unittest.skipUnless(PYPROJECT.is_file(), "pyproject.toml is not beside the tests")
class ExtrasMetadataTests(unittest.TestCase):
    def test_the_crewai_extra_brings_in_langchain_core(self) -> None:
        project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]

        self.assertTrue(
            any(requirement.startswith("langchain-core") for requirement in resolved_requirements(project, "crewai"))
        )


if __name__ == "__main__":
    unittest.main()
