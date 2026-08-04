"""Guards against re-shipping a Python version nothing in CI exercises.

Regression: the langchain integration's pyproject once advertised Python 3.14
support that nothing tested (asyncio.get_event_loop() broke there), discovered
only after release. CI (.github/workflows/ci.yml) runs client-tests on 3.11
only and langchain-tests/host-tests on 3.11 and 3.14 -- no job ever touches
3.10, so a floor of ">=3.10" plus a 3.10 classifier is the same class of
unbacked claim. These two published pyprojects must not repeat it.
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _project_table(relative: str) -> dict:
    path = REPO_ROOT / relative
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


class ClientPyprojectPythonVersionTests(unittest.TestCase):
    def test_requires_python_floor_is_311(self) -> None:
        self.assertEqual(_project_table("client/pyproject.toml")["requires-python"], ">=3.11")

    def test_no_classifier_advertises_the_untested_310(self) -> None:
        classifiers = _project_table("client/pyproject.toml")["classifiers"]
        self.assertNotIn("Programming Language :: Python :: 3.10", classifiers)

    def test_classifiers_still_cover_311_through_314(self) -> None:
        classifiers = _project_table("client/pyproject.toml")["classifiers"]
        for minor in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn(f"Programming Language :: Python :: {minor}", classifiers)


class McpMetapackagePyprojectPythonVersionTests(unittest.TestCase):
    def test_requires_python_floor_matches_the_client(self) -> None:
        self.assertEqual(
            _project_table("packaging/auto-browser-mcp/pyproject.toml")["requires-python"],
            ">=3.11",
        )


if __name__ == "__main__":
    unittest.main()
