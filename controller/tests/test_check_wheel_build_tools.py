"""scripts/check_wheel_build_tools.py, which holds build, pip and what built each wheel to the versions the lock pins.

The release workflow, CI's package-build job and the release audit build the packages with the backends their
pyproject.toml files name; the lock pins those backends, build and pip, and this check reads the build and pip installed
in the Python that runs it and each wheel's own record of what built it. The wheels here are tiny zip files written by
the test, with the WHEEL file each backend writes, and the installed build and pip are a stub of
importlib.metadata.version, except in the one test that runs the script as a command.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_wheel_build_tools.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.parent.is_dir(), reason="scripts/ is not shipped in the controller image")

# As uv writes the lock: each pin on a line of its own, with what pulled it in below.
LOCK = """\
build==1.6.1
    # via -r requirements-dev.txt
hatchling==1.33.0
    # via -r requirements-build.txt
packaging==26.0
    # via
    #   build
    #   hatchling
pip==26.3
    # via pip-api
setuptools==84.2.1
    # via -r requirements-build.txt
"""
# Each backend's Generator line as it writes it, read from a real wheel of each.
SETUPTOOLS = "setuptools (84.2.1)"
HATCHLING = "hatchling 1.33.0"
# The build and pip the stubbed importlib.metadata.version reports as installed: the versions LOCK pins.
INSTALLED = {"build": "1.6.1", "pip": "26.3"}


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("check_wheel_build_tools_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def installed(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """What importlib.metadata.version reports for build and pip, as LOCK pins them; a test may change it."""
    versions = dict(INSTALLED)
    real = importlib.metadata.version

    def version(name: str) -> str:
        if name not in INSTALLED:
            return real(name)
        if name not in versions:
            raise importlib.metadata.PackageNotFoundError(name)
        return versions[name]

    monkeypatch.setattr(importlib.metadata, "version", version)
    return versions


def _wheel(directory: Path, name: str, generator: str | None) -> Path:
    """A wheel holding only its .dist-info, whose WHEEL file names ``generator`` (None: no Generator line)."""
    path = directory / f"{name}-1.0.0-py3-none-any.whl"
    lines = ["Wheel-Version: 1.0", *([f"Generator: {generator}"] if generator is not None else [])]
    lines += ["Root-Is-Purelib: true", "Tag: py3-none-any"]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}-1.0.0.dist-info/WHEEL", "\n".join(lines) + "\n")
        archive.writestr(f"{name}-1.0.0.dist-info/METADATA", f"Metadata-Version: 2.4\nName: {name}\nVersion: 1.0.0\n")
    return path


def _run(check, capsys: pytest.CaptureFixture[str], tmp_path: Path, wheels: list[Path], lock: str = LOCK):
    """The check's exit status, its lines for build and pip, and its lines for the wheels, given ``lock`` and ``wheels``."""
    lock_path = tmp_path / "constraints.txt"
    lock_path.write_text(lock, encoding="utf-8")
    status = check.main([str(lock_path), *map(str, wheels)])
    lines = capsys.readouterr().out.splitlines()
    return status, lines[:2], lines[2:]


def _one_of_each(directory: Path) -> list[Path]:
    """A wheel built by setuptools and one built by hatchling, each by the version LOCK pins."""
    return [
        _wheel(directory, "auto_browser_client", SETUPTOOLS),
        _wheel(directory, "auto_browser_langchain", HATCHLING),
    ]


def test_wheels_built_by_the_pinned_backends_pass(check, capsys, tmp_path: Path) -> None:
    status, frontend, lines = _run(check, capsys, tmp_path, _one_of_each(tmp_path))
    assert status == 0, frontend + lines
    assert [line.split()[:3] for line in frontend] == [["ok", "build", "1.6.1"], ["ok", "pip", "26.3"]], frontend
    assert len(lines) == 2 and all(line.startswith("ok ") for line in lines), lines
    assert "auto_browser_client-1.0.0-py3-none-any.whl" in lines[0] and "setuptools 84.2.1" in lines[0]
    assert "auto_browser_langchain-1.0.0-py3-none-any.whl" in lines[1] and "hatchling 1.33.0" in lines[1]


@pytest.mark.parametrize(
    ("generator", "built", "pinned"),
    [("setuptools (84.2.0)", "84.2.0", "setuptools==84.2.1"), ("hatchling 1.32.0", "1.32.0", "hatchling==1.33.0")],
)
def test_another_version_fails_and_names_both(
    check, capsys, tmp_path: Path, generator: str, built: str, pinned: str
) -> None:
    status, _, lines = _run(check, capsys, tmp_path, [_wheel(tmp_path, "pkg", generator)])
    assert status == 1
    assert len(lines) == 1 and lines[0].startswith("FAIL "), lines
    assert built in lines[0] and pinned in lines[0], lines


@pytest.mark.parametrize(
    "generator",
    [
        "bdist_wheel (0.45.1)",  # the wheel package's, which setuptools used to write
        "flit 3.12.0",
        "setuptools 84.2.1",  # hatchling's form with setuptools' name
        "hatchling (1.33.0)",  # setuptools' form with hatchling's name
        "",
        None,
    ],
)
def test_an_unrecognised_generator_fails(check, capsys, tmp_path: Path, generator: str | None) -> None:
    status, _, lines = _run(check, capsys, tmp_path, [_wheel(tmp_path, "pkg", generator)])
    assert status == 1
    assert len(lines) == 1 and lines[0].startswith("FAIL "), lines


@pytest.mark.parametrize(
    "lock",
    [
        LOCK.replace("hatchling==1.33.0\n", ""),
        LOCK.replace("hatchling==1.33.0", "hatchling>=1.27"),
        LOCK + "hatchling==1.27.0 ; python_version < '3.12'\n",
    ],
    ids=["no-pin", "a-range", "two-pins"],
)
def test_a_lock_without_one_pin_of_the_backend_fails(check, capsys, tmp_path: Path, lock: str) -> None:
    status, _, lines = _run(check, capsys, tmp_path, _one_of_each(tmp_path), lock)
    assert status == 1
    assert lines[0].startswith("ok "), lines
    # The count, not only the name: a check that kept one of two pins would fail here on the version instead.
    assert lines[1].startswith("FAIL ") and f"has {lock.count('hatchling==')} hatchling== pins" in lines[1], lines


def _unreadable(directory: Path, kind: str) -> Path:
    """A .whl the check cannot read one Generator line from, as ``kind`` says; any line it holds names the pinned one."""
    path = directory / "pkg-1.0.0-py3-none-any.whl"
    if kind == "not-a-zip":
        path.write_bytes(b"not a zip file\n")
        return path
    wheel = f"Wheel-Version: 1.0\nGenerator: {SETUPTOOLS}\n".encode()
    contents = {
        "two-generator-lines": wheel + f"Generator: {SETUPTOOLS}\n".encode(),
        "not-utf-8": wheel + b"Tag: \xff\n",
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("pkg-1.0.0.dist-info/WHEEL", contents.get(kind, wheel))
        if kind == "two-wheel-files":
            archive.writestr("other-1.0.0.dist-info/WHEEL", wheel)
    return path


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("not-a-zip", "File is not a zip file"),
        ("two-wheel-files", "holds 2 *.dist-info/WHEEL files"),
        ("two-generator-lines", "holds 2 Generator lines"),
        ("not-utf-8", "codec can't decode"),
    ],
)
def test_a_wheel_without_one_readable_generator_line_fails(
    check, capsys, tmp_path: Path, kind: str, reason: str
) -> None:
    status, _, lines = _run(check, capsys, tmp_path, [_unreadable(tmp_path, kind)])
    assert status == 1
    assert len(lines) == 1 and lines[0].startswith("FAIL ") and reason in lines[0], lines


@pytest.mark.parametrize("tool", ["build", "pip"])
def test_a_build_or_pip_other_than_the_lock_pins_fails_and_names_both(
    check, capsys, tmp_path: Path, installed: dict[str, str], tool: str
) -> None:
    installed[tool] = "1.0"
    status, frontend, lines = _run(check, capsys, tmp_path, _one_of_each(tmp_path))
    assert status == 1
    failed, passed = (frontend[0], frontend[1]) if tool == "build" else (frontend[1], frontend[0])
    assert failed.startswith(f"FAIL {tool} 1.0 ") and f"{tool}=={INSTALLED[tool]}" in failed, frontend
    assert passed.startswith("ok "), frontend
    assert all(line.startswith("ok ") for line in lines), lines


@pytest.mark.parametrize("tool", ["build", "pip"])
def test_a_build_or_pip_that_is_not_installed_fails(
    check, capsys, tmp_path: Path, installed: dict[str, str], tool: str
) -> None:
    del installed[tool]
    status, frontend, _ = _run(check, capsys, tmp_path, _one_of_each(tmp_path))
    assert status == 1
    assert [line for line in frontend if line.startswith("FAIL ")] == [
        f"FAIL {tool}: not installed in {sys.executable}"
    ], frontend


@pytest.mark.parametrize(
    ("tool", "lock"),
    [
        ("pip", LOCK.replace("pip==26.3\n", "")),
        ("pip", LOCK.replace("pip==26.3", "pip>=26")),
        ("build", LOCK + "build==1.5.0 ; python_version < '3.12'\n"),
    ],
    ids=["no-pin", "a-range", "two-pins"],
)
def test_a_lock_without_one_pin_of_build_or_pip_fails(check, capsys, tmp_path: Path, tool: str, lock: str) -> None:
    status, frontend, _ = _run(check, capsys, tmp_path, _one_of_each(tmp_path), lock)
    assert status == 1
    failed = [line for line in frontend if line.startswith("FAIL ")]
    assert len(failed) == 1 and f"has {lock.count(f'{tool}==')} {tool}== pins" in failed[0], frontend


def test_a_wheel_path_that_names_no_file_fails(check, capsys, tmp_path: Path) -> None:
    # As a shell passes a glob that matched nothing: the pattern itself.
    status, _, lines = _run(check, capsys, tmp_path, [tmp_path / "wheelhouse" / "*.whl"])
    assert status == 1
    assert len(lines) == 1 and lines[0].startswith("FAIL "), lines


def test_no_wheel_given_fails(check, capsys, tmp_path: Path) -> None:
    lock_path = tmp_path / "constraints.txt"
    lock_path.write_text(LOCK, encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        check.main([str(lock_path)])
    assert caught.value.code != 0


def test_the_script_run_as_a_command_exits_1_when_any_line_fails(tmp_path: Path) -> None:
    # Unstubbed, in a process of its own: the lock pins the build and pip this interpreter has.
    real = {tool: importlib.metadata.distribution(tool).version for tool in INSTALLED}
    lock = LOCK.replace("build==1.6.1", f"build=={real['build']}").replace("pip==26.3", f"pip=={real['pip']}")
    lock_path = tmp_path / "constraints.txt"
    lock_path.write_text(lock, encoding="utf-8")
    other_pip = tmp_path / "other-pip.txt"
    other_pip.write_text(lock.replace(f"pip=={real['pip']}", "pip==1.0"), encoding="utf-8")
    good = _wheel(tmp_path, "auto_browser_client", SETUPTOOLS)
    bad = _wheel(tmp_path, "auto_browser_mcp", "setuptools (80.0.0)")

    def run(lock: Path, *wheels: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_SCRIPT), str(lock), *map(str, wheels)], capture_output=True, text=True
        )

    passed, failed, pip_failed = run(lock_path, good), run(lock_path, good, bad), run(other_pip, good)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert len(failed.stdout.splitlines()) == 4 and "80.0.0" in failed.stdout
    assert pip_failed.returncode == 1, pip_failed.stdout + pip_failed.stderr
    assert f"FAIL pip {real['pip']} " in pip_failed.stdout and "pip==1.0" in pip_failed.stdout
