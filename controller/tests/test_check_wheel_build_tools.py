"""scripts/check_wheel_build_tools.py, which reads what built each wheel and holds it to the version the lock pins.

The release workflow, CI's package-build job and the release audit build the packages with the backends their
pyproject.toml files name; the lock pins those backends, and this check reads each wheel's own record of what built it.
The wheels here are tiny zip files written by the test, with the WHEEL file each backend writes.
"""

from __future__ import annotations

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
hatchling==1.33.0
    # via -r requirements-build.txt
packaging==26.0
    # via
    #   build
    #   hatchling
setuptools==84.2.1
    # via -r requirements-build.txt
"""
# Each backend's Generator line as it writes it, read from a real wheel of each.
SETUPTOOLS = "setuptools (84.2.1)"
HATCHLING = "hatchling 1.33.0"


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("check_wheel_build_tools_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    """The check's exit status and the lines it printed, given ``lock`` and ``wheels``."""
    lock_path = tmp_path / "constraints.txt"
    lock_path.write_text(lock, encoding="utf-8")
    status = check.main([str(lock_path), *map(str, wheels)])
    out = capsys.readouterr().out
    return status, out.splitlines()


def _one_of_each(directory: Path) -> list[Path]:
    """A wheel built by setuptools and one built by hatchling, each by the version LOCK pins."""
    return [
        _wheel(directory, "auto_browser_client", SETUPTOOLS),
        _wheel(directory, "auto_browser_langchain", HATCHLING),
    ]


def test_wheels_built_by_the_pinned_backends_pass(check, capsys, tmp_path: Path) -> None:
    status, lines = _run(check, capsys, tmp_path, _one_of_each(tmp_path))
    assert status == 0, lines
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
    status, lines = _run(check, capsys, tmp_path, [_wheel(tmp_path, "pkg", generator)])
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
    status, lines = _run(check, capsys, tmp_path, [_wheel(tmp_path, "pkg", generator)])
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
    status, lines = _run(check, capsys, tmp_path, _one_of_each(tmp_path), lock)
    assert status == 1
    assert lines[0].startswith("ok "), lines
    assert lines[1].startswith("FAIL ") and "hatchling" in lines[1], lines


def test_a_file_that_is_no_wheel_fails(check, capsys, tmp_path: Path) -> None:
    # As a shell passes a glob that matched nothing: the pattern itself.
    status, lines = _run(check, capsys, tmp_path, [tmp_path / "wheelhouse" / "*.whl"])
    assert status == 1
    assert len(lines) == 1 and lines[0].startswith("FAIL "), lines


def test_no_wheel_given_fails(check, capsys, tmp_path: Path) -> None:
    lock_path = tmp_path / "constraints.txt"
    lock_path.write_text(LOCK, encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        check.main([str(lock_path)])
    assert caught.value.code != 0


def test_the_command_runs_as_the_workflows_run_it(tmp_path: Path) -> None:
    lock_path = tmp_path / "constraints.txt"
    lock_path.write_text(LOCK, encoding="utf-8")
    good = _wheel(tmp_path, "auto_browser_client", SETUPTOOLS)
    bad = _wheel(tmp_path, "auto_browser_mcp", "setuptools (80.0.0)")
    passed = subprocess.run([sys.executable, str(_SCRIPT), str(lock_path), str(good)], capture_output=True, text=True)
    failed = subprocess.run(
        [sys.executable, str(_SCRIPT), str(lock_path), str(good), str(bad)], capture_output=True, text=True
    )
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert len(failed.stdout.splitlines()) == 2 and "80.0.0" in failed.stdout
