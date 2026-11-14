"""check_playwright_pins must compare every place the Playwright version is pinned, and refuse a range.

The controller (pip) and browser-node (npm) speak one protocol, so their Playwright versions must be identical. The
check read only controller/requirements.txt and browser-node/package.json, and stripped a range operator there, so
`^1.64.1` passed while npm could install any 1.x, and the lock npm ci installs from, and the constraints file the
image installs under, could pin another version unnoticed.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

try:
    import check_playwright_pins  # noqa: E402
except ModuleNotFoundError as exc:  # scripts/ is not shipped in the controller image
    raise unittest.SkipTest(f"check_playwright_pins unavailable: {exc}") from exc

VERSION = "1.64.1"


def _check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    requirements: str = VERSION,
    constraints: str = VERSION,
    package: str = VERSION,
    lock: str = VERSION,
    lock_core: str = VERSION,
) -> tuple[int, str]:
    paths = {
        "REQUIREMENTS": tmp_path / "requirements.txt",
        "CONSTRAINTS": tmp_path / "constraints.txt",
        "PACKAGE_JSON": tmp_path / "package.json",
        "PACKAGE_LOCK": tmp_path / "package-lock.json",
    }
    paths["REQUIREMENTS"].write_text(f"fastapi==0.1\nplaywright=={requirements}\n", encoding="utf-8")
    paths["CONSTRAINTS"].write_text(
        f"greenlet==3.0\n    # via playwright\nplaywright=={constraints}\n", encoding="utf-8"
    )
    paths["PACKAGE_JSON"].write_text(json.dumps({"dependencies": {"playwright": package}}), encoding="utf-8")
    packages = {
        "": {"dependencies": {"playwright": package}},
        "node_modules/playwright": {"version": lock},
        "node_modules/playwright-core": {"version": lock_core},
    }
    paths["PACKAGE_LOCK"].write_text(json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8")
    for name, path in paths.items():
        monkeypatch.setattr(check_playwright_pins, name, path)
    stderr = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
        code = check_playwright_pins.main()
    return code, stderr.getvalue()


def test_every_pin_equal_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _check(monkeypatch, tmp_path) == (0, "")


@pytest.mark.parametrize("spec", ["^1.64.1", "~1.64.1", ">=1.64.1", "=1.64.1", "1.64.x", "*"])
def test_a_range_in_package_json_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: str) -> None:
    code, report = _check(monkeypatch, tmp_path, package=spec)
    assert code == 1
    assert "package.json" in report and spec in report and "exact" in report


@pytest.mark.parametrize(
    ("pin", "location"),
    [
        ("lock", "package-lock.json"),
        ("lock_core", "package-lock.json"),
        ("constraints", "constraints.txt"),
        ("requirements", "requirements.txt"),
    ],
)
def test_one_pin_behind_fails_naming_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pin: str, location: str
) -> None:
    code, report = _check(monkeypatch, tmp_path, **{pin: "1.64.0"})
    assert code == 1
    behind = [line for line in report.splitlines() if line.rstrip().endswith("1.64.0")]
    assert len(behind) == 1 and location in behind[0], report
