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


_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?")


def normalise(name: str) -> str:
    """A project or extra name as PEP 503 compares it: lower case, runs of `-`, `_` and `.` as one `-`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse(requirement: str) -> tuple[str, list[str]]:
    """The normalised name and the normalised extras of a requirement string; an environment marker is dropped."""
    match = _REQUIREMENT.match(requirement.split(";", 1)[0])
    if match is None:
        raise ValueError(f"not a requirement: {requirement!r}")
    extras = [normalise(extra.strip()) for extra in (match.group(2) or "").split(",") if extra.strip()]
    return normalise(match.group(1)), extras


def resolved_requirements(project: dict, extra: str) -> list[str]:
    """An extra's requirements, with a `<project name>[x,y]` self-reference replaced by those extras' (one level)."""
    name = normalise(project["name"])
    extras = {normalise(key): value for key, value in project["optional-dependencies"].items()}
    resolved: list[str] = []
    for requirement in extras[normalise(extra)]:
        required_name, required_extras = parse(requirement)
        if required_name == name and required_extras:
            for required_extra in required_extras:
                resolved.extend(extras[required_extra])
        else:
            resolved.append(requirement)
    return resolved


def names(requirements: list[str]) -> list[str]:
    return [parse(requirement)[0] for requirement in requirements]


@unittest.skipUnless(PYPROJECT.is_file(), "pyproject.toml is not beside the tests")
class ExtrasMetadataTests(unittest.TestCase):
    def test_the_crewai_extra_brings_in_langchain_core(self) -> None:
        project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]

        self.assertIn("langchain-core", names(resolved_requirements(project, "crewai")))

    def test_a_self_reference_is_resolved_however_it_is_spelled(self) -> None:
        extras = {"langchain": ["langchain-core>=0.2"], "dev": ["pytest"], "crewai": []}
        for reference in (
            "auto-browser-langchain[langchain]",
            "auto_browser_langchain[langchain]",
            "Auto.Browser.LangChain[LangChain]",
            "auto-browser-langchain[ langchain ] ; python_version >= '3.10'",
        ):
            with self.subTest(reference=reference):
                project = {
                    "name": "auto-browser-langchain",
                    "optional-dependencies": {**extras, "crewai": [reference, "crewai>=0.28"]},
                }
                self.assertEqual(names(resolved_requirements(project, "crewai")), ["langchain-core", "crewai"])

    def test_a_self_reference_with_several_extras_brings_in_all_of_them(self) -> None:
        project = {
            "name": "auto-browser-langchain",
            "optional-dependencies": {
                "langchain": ["langchain-core>=0.2"],
                "dev": ["pytest"],
                "all": ["auto-browser-langchain[langchain,dev]"],
            },
        }

        self.assertEqual(names(resolved_requirements(project, "all")), ["langchain-core", "pytest"])

    def test_a_different_package_that_starts_with_langchain_core_does_not_count(self) -> None:
        project = {"name": "p", "optional-dependencies": {"crewai": ["langchain-core-extras>=1", "crewai"]}}

        self.assertNotIn("langchain-core", names(resolved_requirements(project, "crewai")))


if __name__ == "__main__":
    unittest.main()
