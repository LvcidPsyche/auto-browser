"""The tools that build the published packages are pinned in controller/constraints.txt, the repository's one lock.

`python -m build` installs each package's build backend fresh, from its pyproject.toml's `[build-system] requires`,
which hold only floors: the release built the packages it uploads with whatever backend was newest that day. The lock
pins the backends and the frontend, and every pin meets the floors the four pyproject.toml files set.

Every workflow installs the frontend under the lock and hands it to each build, and each job that builds runs
scripts/check_wheel_build_tools.py on the wheels after its last build, before anything is uploaded. The workflow tests
find the commands by what they run, not by step names, so a build added later without the lock fails them.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from tests.test_python_floor_is_tested import PYPROJECTS

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK = REPO_ROOT / "controller" / "constraints.txt"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
# The lock and the check as the workflows name them, from the repository's root, where their steps run.
LOCK_ARG = "controller/constraints.txt"
CHECK = "scripts/check_wheel_build_tools.py"

# The controller image ships only `app/` and `tests/`: the workflows and the other packages' pyproject.toml files are
# not in it.
pytestmark = pytest.mark.skipif(
    not (WORKFLOWS.is_dir() and all((REPO_ROOT / name).is_file() for name in PYPROJECTS)),
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


def _commands(run: str) -> list[list[str]]:
    """The commands of a step's run text, one per line once each `\\` continuation is joined, each as its words."""
    joined = re.sub(r"\\\r?\n", " ", run)
    return [[word.strip("'\"") for word in line.split()] for line in joined.splitlines() if line.strip()]


def _pairs(words: list[str]) -> set[tuple[str, str]]:
    return set(zip(words, words[1:]))


def _is_package_build(words: list[str]) -> bool:
    """`python -m build`, whatever the interpreter, or build's own `pyproject-build`; not `docker compose build`."""
    return ("-m", "build") in _pairs(words) or "pyproject-build" in words


def _installs_build(words: list[str]) -> bool:
    """A pip install (python -m pip, pip or uv pip) that names `build`, with or without a version."""
    if ("pip", "install") not in _pairs(words):
        return False
    wanted = words[words.index("install") + 1 :]
    return any(re.fullmatch(r"build(\[[^\]]*\])?([<>=!~].*)?", word) for word in wanted)


def _under_the_lock(words: list[str], *options: str) -> bool:
    return any((option, LOCK_ARG) in _pairs(words) for option in options)


def _jobs() -> list[tuple[str, str, dict]]:
    """Every job of every workflow, as (workflow file, job name, job)."""
    found = []
    for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        jobs = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]
        found += [(path.name, name, job) for name, job in jobs.items()]
    return found


def _job_commands(job: dict) -> list[tuple[int, list[str]]]:
    """Each command the job's steps run, in order, with its step's index."""
    steps = job.get("steps", [])
    return [(index, words) for index, step in enumerate(steps) for words in _commands(step.get("run", ""))]


def _all_commands() -> list[tuple[str, str, list[str]]]:
    return [(workflow, name, words) for workflow, name, job in _jobs() for _, words in _job_commands(job)]


@pytest.mark.parametrize(
    ("run", "builds"),
    [
        ("docker compose build controller\n", [False]),
        ("INSTALL_DEV_DEPS=true docker compose build controller\n", [False]),
        ("python -m build --outdir dist client\n", [True]),
        ("cd client && python3 -m build \\\n  --wheel .\n", [True]),
        ("pyproject-build --outdir dist client\n", [True]),
        ("python -m pip install build\npython -m buildx\n", [False, False]),
    ],
)
def test_a_package_build_is_found_by_what_it_runs(run: str, builds: list[bool]) -> None:
    assert [_is_package_build(words) for words in _commands(run)] == builds


@pytest.mark.parametrize(
    ("run", "installs"),
    [
        ("python -m pip install build\n", True),
        ('python -m pip install "build==1.6.1"\n', True),
        ("uv pip install --system \\\n  build pip-audit\n", True),
        ("python -m pip install pip-audit -c controller/constraints.txt\n", False),
        ("python -m pip install buildx\n", False),
        ("python -m build --outdir dist client\n", False),
    ],
)
def test_an_install_of_build_is_found_by_what_it_runs(run: str, installs: bool) -> None:
    assert [_installs_build(words) for words in _commands(run)] == [installs]


def test_a_continued_line_is_read_as_one_command() -> None:
    run = (
        "python -m build --outdir dist client \\\n  --dependency-constraints-txt controller/constraints.txt\nls dist\n"
    )
    commands = _commands(run)
    assert len(commands) == 2
    assert _under_the_lock(commands[0], "--dependency-constraints-txt")


def test_every_pip_install_of_build_is_under_the_lock() -> None:
    installs = [(workflow, name, words) for workflow, name, words in _all_commands() if _installs_build(words)]
    assert {"release.yml", "ci.yml"} <= {workflow for workflow, *_ in installs}, installs
    for workflow, name, words in installs:
        assert _under_the_lock(words, "-c", "--constraint"), f"{workflow} {name}: {' '.join(words)}"


def test_every_package_build_is_given_the_lock() -> None:
    builds = [(workflow, name, words) for workflow, name, words in _all_commands() if _is_package_build(words)]
    assert {"release.yml", "ci.yml"} <= {workflow for workflow, *_ in builds}, builds
    for workflow, name, words in builds:
        assert _under_the_lock(words, "--dependency-constraints-txt"), f"{workflow} {name}: {' '.join(words)}"


def test_each_job_that_builds_checks_every_wheel_after_its_last_build_and_before_any_upload() -> None:
    building = []
    for workflow, name, job in _jobs():
        commands = _job_commands(job)
        builds = [position for position, (_, words) in enumerate(commands) if _is_package_build(words)]
        if not builds:
            continue
        building.append((workflow, name))
        checks = [position for position, (_, words) in enumerate(commands) if CHECK in words]
        assert len(checks) == 1, f"{workflow} {name} runs {CHECK} {len(checks)} times"
        assert checks[0] > builds[-1], f"{workflow} {name} runs {CHECK} before its last build"
        check_step, check = commands[checks[0]]
        assert check[check.index(CHECK) + 1] == LOCK_ARG, " ".join(check)
        for position in builds:
            words = commands[position][1]
            assert "--outdir" in words, " ".join(words)
            wheels = f"{words[words.index('--outdir') + 1]}/*.whl"
            assert wheels in check, f"{workflow} {name}: {CHECK} does not read {wheels}"
        uploads = [
            i for i, step in enumerate(job["steps"]) if step.get("uses", "").startswith("actions/upload-artifact")
        ]
        assert all(check_step < upload for upload in uploads), f"{workflow} {name} uploads before {CHECK} runs"
        if workflow == "release.yml":
            assert uploads, "release.yml's build job uploads nothing: the publish jobs read its artifact"
    assert {("release.yml", "build"), ("ci.yml", "package-build")} <= set(building), building
