"""release_audit must build and inspect every package the release workflow publishes.

release.yml builds client/, integrations/langchain and packaging/auto-browser-mcp. The
audit used to build only three of the four directories it lists today, so a broken
metapackage first failed at tag time.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

try:
    import release_audit  # noqa: E402
except ModuleNotFoundError as exc:  # scripts/ is not shipped in the controller image
    raise unittest.SkipTest(f"release_audit unavailable: {exc}") from exc

RELEASE_WORKFLOW = _REPO / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = _REPO / ".github" / "workflows" / "ci.yml"
GOOD_METADATA = (
    "Metadata-Version: 2.4\nName: auto-browser-mcp\nVersion: 1.0.0\nRequires-Dist: auto-browser-client>=1.0.0\n"
)
GOOD_ENTRY_POINTS = "[console_scripts]\nauto-browser-mcp = auto_browser_client.mcp_bridge:main\n"


def _write_wheel(wheelhouse: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(wheelhouse / "auto_browser_mcp-1.0.0-py3-none-any.whl", "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _good_files() -> dict[str, str]:
    return {
        "auto_browser_mcp-1.0.0.dist-info/METADATA": GOOD_METADATA,
        "auto_browser_mcp-1.0.0.dist-info/entry_points.txt": GOOD_ENTRY_POINTS,
        "auto_browser_mcp-1.0.0.dist-info/RECORD": "",
    }


class ReleaseWorkflowCoverageTests(unittest.TestCase):
    @unittest.skipUnless(RELEASE_WORKFLOW.is_file(), "release workflow is not in this tree")
    def test_every_package_release_yml_builds_is_built_by_the_audit(self) -> None:
        built = re.findall(r"python -m build --outdir dist (\S+)", RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        self.assertIn("packaging/auto-browser-mcp", built)
        missing = [directory for directory in built if directory not in release_audit.WHEEL_PACKAGES]
        self.assertEqual(missing, [])


class InspectMcpWheelTests(unittest.TestCase):
    def _inspect(self, files: dict[str, str] | None) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wheelhouse = Path(temp_dir)
            if files is not None:
                _write_wheel(wheelhouse, files)
            release_audit._inspect_mcp_wheel(wheelhouse)

    def test_accepts_the_expected_metapackage(self) -> None:
        self._inspect(_good_files())

    def test_rejects_a_missing_wheel(self) -> None:
        with self.assertRaisesRegex(SystemExit, "MCP metapackage wheel was not built"):
            self._inspect(None)

    def test_rejects_python_modules(self) -> None:
        files = _good_files() | {"stray.py": "print('x')\n"}
        with self.assertRaisesRegex(SystemExit, "stray.py"):
            self._inspect(files)

    def test_rejects_missing_client_dependency(self) -> None:
        files = _good_files() | {"auto_browser_mcp-1.0.0.dist-info/METADATA": "Name: auto-browser-mcp\n"}
        with self.assertRaisesRegex(SystemExit, "Requires-Dist: auto-browser-client"):
            self._inspect(files)

    def test_rejects_wrong_console_script(self) -> None:
        files = _good_files() | {
            "auto_browser_mcp-1.0.0.dist-info/entry_points.txt": (
                "[console_scripts]\nauto-browser-mcp = somewhere_else:main\n"
            )
        }
        with self.assertRaisesRegex(SystemExit, "auto_browser_client.mcp_bridge:main"):
            self._inspect(files)

    def test_rejects_missing_entry_points(self) -> None:
        files = _good_files()
        del files["auto_browser_mcp-1.0.0.dist-info/entry_points.txt"]
        with self.assertRaisesRegex(SystemExit, "auto_browser_client.mcp_bridge:main"):
            self._inspect(files)


def _audit_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[list[str], Path | None, str | None]]:
    """The commands main() runs, in order, with every step stubbed: `_run` records instead of running.

    Each is kept with its directory and the PYTHONPATH it sets (None: it sets none), never its whole environment, so
    a failing assertion cannot print the environment's values.
    """
    calls: list[tuple[list[str], Path | None, str | None]] = []

    def record(command: list[object], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        calls.append(([str(part) for part in command], cwd, None if env is None else env.get("PYTHONPATH")))

    monkeypatch.setattr(release_audit, "_run", record)
    monkeypatch.setattr(release_audit, "_check_launch_files", lambda: None)
    monkeypatch.setattr(release_audit, "_require", lambda command: None)
    monkeypatch.setattr(release_audit, "_inspect_controller_wheel", lambda wheelhouse: None)
    monkeypatch.setattr(release_audit, "_inspect_mcp_wheel", lambda wheelhouse: None)
    monkeypatch.setattr(release_audit.os, "chdir", lambda path: None)
    # The secret scan is the one subprocess main() starts itself: it finds nothing.
    monkeypatch.setattr(release_audit.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["release_audit.py", "--skip-advisory-check", "--skip-doctor"])
    assert release_audit.main() == 0
    return calls


def _ci_job(name: str) -> str:
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text)
    assert match, f"no {name} job in ci.yml"
    return match.group(1)


@pytest.mark.skipif(not CI_WORKFLOW.is_file(), reason="ci.yml is not in this tree")
@pytest.mark.parametrize("job_name", ["client-tests", "langchain-tests"])
def test_the_audit_runs_each_package_suite_as_ci_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_name: str
) -> None:
    job = _ci_job(job_name)
    install = re.search(r"'(\d+) hours ago'.*?-e '?\./([^\[\s']+)\[([^\]]+)\]", job, flags=re.S)
    assert install, f"{job_name} installs no package with extras under a hold"
    hold_hours, directory, extras = int(install.group(1)), install.group(2), install.group(3)
    assert f"cd {directory}\n" in job
    module = re.search(r"--cov=(\S+)", job).group(1)
    gate = re.search(r"--cov-fail-under=(\d+)", job).group(1)
    pythonpath = "." if "PYTHONPATH=. python -m pytest tests" in job else None

    calls = _audit_commands(monkeypatch, tmp_path)

    expected = [
        "-m",
        "pytest",
        "tests",
        "-q",
        f"--cov={module}",
        "--cov-report=term-missing",
        f"--cov-fail-under={gate}",
    ]
    suites = [i for i, (command, cwd, _) in enumerate(calls) if cwd == release_audit.ROOT / directory]
    assert len(suites) == 1, f"the audit runs no single suite in {directory}: {calls}"
    suite_command, _, suite_pythonpath = calls[suites[0]]
    assert suite_command[1:-2] == expected
    assert suite_command[-2] == "--basetemp" and Path(suite_command[-1]).is_relative_to(tmp_path)
    assert suite_pythonpath == pythonpath
    interpreter = Path(suite_command[0])

    # The suite's interpreter is a fresh environment the audit made, with the package installed in it as CI installs
    # it: editable, with its extras, and nothing newer than the hold.
    installs = [i for i, (command, _, _) in enumerate(calls) if f"./{directory}[{extras}]" in command]
    assert len(installs) == 1 and installs[0] < suites[0], calls
    install_command = calls[installs[0]][0]
    assert install_command[:3] == ["uv", "pip", "install"]
    assert install_command[install_command.index("--python") + 1] == str(interpreter)
    assert install_command[-2:] == ["-e", f"./{directory}[{extras}]"]
    cutoff = datetime.strptime(install_command[install_command.index("--exclude-newer") + 1], "%Y-%m-%dT%H:%M:%SZ")
    expected_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hold_hours)
    assert abs(cutoff - expected_cutoff) < timedelta(minutes=5)
    venvs = [
        i
        for i, (command, _, _) in enumerate(calls)
        if command[1:3] == ["-m", "venv"] and interpreter.is_relative_to(Path(command[-1]))
    ]
    assert len(venvs) == 1 and venvs[0] < installs[0], calls


def test_a_failing_step_says_which_command_failed_and_where(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        release_audit._run([sys.executable, "-c", "raise SystemExit(3)"], cwd=tmp_path)
    message = str(caught.value.code)
    assert message.startswith("Release audit failed:")
    assert "-c raise SystemExit(3)" in message
    assert str(tmp_path) in message
    assert "exited with status 3" in message


if __name__ == "__main__":
    unittest.main()
