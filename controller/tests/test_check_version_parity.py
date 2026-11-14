"""check_version_parity must hold the MCP metapackage's client floor to the release's version.

The metapackage only re-exports the client, so a release of it must require that release's client: bump_version.py
moves the `auto-browser-client>=` floor with the versions, and the parity check now fails when a hand edit or a
missed bump leaves it behind.
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
    import bump_version  # noqa: E402
    import check_version_parity  # noqa: E402
except ModuleNotFoundError as exc:  # scripts/ is not shipped in the controller image
    raise unittest.SkipTest(f"check_version_parity unavailable: {exc}") from exc

METAPACKAGE = "packaging/auto-browser-mcp/pyproject.toml"


def _tree(root: Path, version: str, floor: str) -> None:
    """Every file the parity check reads, at `version`, with the metapackage requiring the client at `floor`."""
    for relative in check_version_parity.PYPROJECTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        dependencies = f'dependencies = ["auto-browser-client>={floor}"]\n' if relative == METAPACKAGE else ""
        path.write_text(f'[project]\nname = "x"\nversion = "{version}"\n{dependencies}', encoding="utf-8")
    for relative in check_version_parity.DUNDER_VERSIONS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    node = root / "browser-node"
    node.mkdir()
    (node / "package.json").write_text(json.dumps({"version": version}, indent=2), encoding="utf-8")
    lock = {"version": version, "packages": {"": {"version": version}}}
    (node / "package-lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")


def _main(monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[int, str]:
    monkeypatch.setattr(check_version_parity, "REPO_ROOT", root)
    stderr = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
        code = check_version_parity.main()
    return code, stderr.getvalue()


def test_a_floor_below_the_version_fails_with_the_file_and_both_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _tree(tmp_path, "1.16.0", "1.15.0")
    code, report = _main(monkeypatch, tmp_path)
    assert code == 1
    assert "most common: 1.16.0" in report
    floor_lines = [line for line in report.splitlines() if "auto-browser-client>=" in line]
    assert len(floor_lines) == 1 and floor_lines[0].startswith("->"), report
    assert METAPACKAGE in floor_lines[0] and floor_lines[0].endswith("1.15.0")


def test_a_floor_equal_to_the_version_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _tree(tmp_path, "1.16.0", "1.16.0")
    assert _main(monkeypatch, tmp_path) == (0, "")


def test_a_metapackage_without_the_floor_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _tree(tmp_path, "1.16.0", "1.16.0")
    path = tmp_path / METAPACKAGE
    path.write_text(path.read_text(encoding="utf-8").replace(">=1.16.0", "==1.16.0"), encoding="utf-8")
    with pytest.raises(SystemExit, match="auto-browser-client>="):
        _main(monkeypatch, tmp_path)


def test_bump_version_moves_the_floor_with_the_versions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _tree(tmp_path, "1.16.0", "1.15.0")
    monkeypatch.setattr(check_version_parity, "REPO_ROOT", tmp_path)
    bump_version.bump("1.17.0", root=tmp_path)
    assert {version for _, version in check_version_parity.collect()} == {"1.17.0"}
