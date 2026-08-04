"""No package may claim a Python version CI does not test.

This exact pattern has now shipped two bugs:

  * `auto-browser-langchain` advertised 3.14 in `requires-python` and its
    classifiers while nothing tested it, so `asyncio.get_event_loop()` raising
    on 3.14 broke the sync `_run` path in production (fixed in v1.4.2).
  * `auto-browser-client` and `auto-browser-mcp` advertised 3.10 to PyPI while
    CI tested only 3.11 and 3.14 (corrected in v1.5.2).

Every previous fix corrected the *instances*. This asserts the invariant, so
lowering a floor without adding the matching CI job fails here instead of on a
user's machine.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

PYPROJECTS = (
    "controller/pyproject.toml",
    "client/pyproject.toml",
    "integrations/langchain/pyproject.toml",
    "packaging/auto-browser-mcp/pyproject.toml",
)

# Repo-layout checks: the controller image ships only `app/` and `tests/`, so
# neither the workflow nor the sibling packages exist inside the container.
pytestmark = pytest.mark.skipif(
    not CI_WORKFLOW.is_file(),
    reason="repo checkout not available (running inside the controller image)",
)


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _ci_tested_versions() -> set[tuple[int, ...]]:
    """Every `python-version` value the CI workflow runs jobs on."""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    found: set[tuple[int, ...]] = set()
    # matrix form: python-version: ["3.11", "3.14"]
    for match in re.finditer(r"python-version:\s*\[([^\]]+)\]", text):
        for raw in match.group(1).split(","):
            cleaned = raw.strip().strip('"').strip("'")
            if cleaned:
                found.add(_version_tuple(cleaned))
    # scalar form: python-version: "3.11"
    for match in re.finditer(r'python-version:\s*["\']([\d.]+)["\']', text):
        found.add(_version_tuple(match.group(1)))
    return found


def test_ci_python_versions_were_parsed() -> None:
    """Guard the guard — an unparsed workflow would make every case vacuous."""
    versions = _ci_tested_versions()
    assert versions, f"no python-version entries parsed from {CI_WORKFLOW}"
    assert (3, 11) in versions


@pytest.mark.parametrize("relative", PYPROJECTS)
def test_requires_python_floor_is_actually_tested(relative: str) -> None:
    path = REPO_ROOT / relative
    if not path.is_file():
        pytest.skip(f"{relative} not present")

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    requires = data.get("project", {}).get("requires-python")
    assert requires, f"{relative} declares no requires-python"

    match = re.search(r">=\s*([\d.]+)", requires)
    assert match, f"{relative} has an unparseable requires-python: {requires!r}"
    floor = _version_tuple(match.group(1))

    tested = _ci_tested_versions()
    assert floor in tested, (
        f"{relative} declares requires-python {requires!r}, but CI never runs "
        f"Python {'.'.join(str(p) for p in floor)}. Either add it to the CI "
        f"matrix or raise the floor — an untested floor is a promise to users "
        f"that nothing verifies."
    )


@pytest.mark.parametrize("relative", PYPROJECTS)
def test_python_classifiers_do_not_exceed_the_floor(relative: str) -> None:
    """A `Python :: 3.10` classifier with a `>=3.11` floor misleads PyPI users."""
    path = REPO_ROOT / relative
    if not path.is_file():
        pytest.skip(f"{relative} not present")

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    requires = project.get("requires-python", "")
    match = re.search(r">=\s*([\d.]+)", requires)
    if not match:
        pytest.skip(f"{relative} has no >= floor")
    floor = _version_tuple(match.group(1))

    for classifier in project.get("classifiers", []):
        found = re.fullmatch(r"Programming Language :: Python :: (\d+\.\d+)", classifier)
        if not found:
            continue
        advertised = _version_tuple(found.group(1))
        assert advertised >= floor, (
            f"{relative} ships classifier {classifier!r} but requires-python is "
            f"{requires!r} — pip would refuse to install on that version."
        )
