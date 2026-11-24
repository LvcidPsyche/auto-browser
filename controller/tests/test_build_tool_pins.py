"""The tools that build the published packages are pinned in controller/constraints.txt, the repository's one lock.

`python -m build` installs each package's build backend fresh, from its pyproject.toml's `[build-system] requires`,
which hold only floors: the release built the packages it uploads with whatever backend was newest that day. The lock
pins the backends and the frontend, and every pin meets the floors the four pyproject.toml files set.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from tests.test_python_floor_is_tested import PYPROJECTS

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK = REPO_ROOT / "controller" / "constraints.txt"

# The controller image ships only `app/` and `tests/`: the other packages' pyproject.toml files are not in it.
pytestmark = pytest.mark.skipif(
    not all((REPO_ROOT / name).is_file() for name in PYPROJECTS),
    reason="repo checkout not available (running inside the controller image)",
)


def _lock_pins() -> dict[str, list[str]]:
    """Every version the lock pins with == for each package, by its canonical name."""
    pins: dict[str, list[str]] = {}
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        for specifier in requirement.specifier:
            if specifier.operator == "==":
                pins.setdefault(canonicalize_name(requirement.name), []).append(specifier.version)
    return pins


def _backend_requirements() -> list[tuple[str, Requirement]]:
    """Each `[build-system] requires` entry of the four pyproject.toml files, with the file that names it."""
    found = []
    for name in PYPROJECTS:
        with (REPO_ROOT / name).open("rb") as handle:
            requires = tomllib.load(handle)["build-system"]["requires"]
        found += [(name, Requirement(entry)) for entry in requires]
    return found


@pytest.mark.parametrize("backend", ["setuptools", "hatchling"])
def test_the_lock_pins_each_build_backend(backend: str) -> None:
    named = {canonicalize_name(requirement.name) for _, requirement in _backend_requirements()}
    assert backend in named, f"no pyproject.toml requires {backend}"
    assert len(_lock_pins().get(backend, [])) == 1, f"controller/constraints.txt pins no single {backend}== version"


def test_each_backend_pin_meets_every_floor_that_names_it() -> None:
    pins = _lock_pins()
    for pyproject, requirement in _backend_requirements():
        versions = pins.get(canonicalize_name(requirement.name), [])
        assert len(versions) == 1, f"{pyproject} requires {requirement}, which controller/constraints.txt does not pin"
        assert requirement.specifier.contains(versions[0], prereleases=True), (
            f"controller/constraints.txt pins {requirement.name}=={versions[0]}, outside {pyproject}'s {requirement}"
        )


def test_the_lock_pins_the_build_frontend() -> None:
    assert len(_lock_pins().get("build", [])) == 1
