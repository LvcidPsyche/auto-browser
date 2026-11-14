"""release_audit must build and inspect every package the release workflow publishes.

release.yml builds client/, integrations/langchain and packaging/auto-browser-mcp. The
audit used to build only three of the four directories it lists today, so a broken
metapackage first failed at tag time.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from collections.abc import Callable
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

    @unittest.skipUnless(RELEASE_WORKFLOW.is_file(), "release workflow is not in this tree")
    def test_the_build_job_requires_the_release_notes_before_anything_is_published(self) -> None:
        # github-release reads the notes only after publish-pypi has published: a tag without its CHANGELOG section
        # failed there, with the packages already on PyPI.
        text = RELEASE_WORKFLOW.read_text(encoding="utf-8")

        def job(name: str) -> str:
            match = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text)
            self.assertIsNotNone(match, f"no {name} job in release.yml")
            return match.group(1)

        build = job("build")
        notes = build.find('run: python scripts/extract_changelog.py "${GITHUB_REF_NAME#v}" > /dev/null\n')
        self.assertNotEqual(notes, -1, "the build job does not check for the tag's CHANGELOG section")
        self.assertLess(notes, build.index("python -m build"))
        self.assertIn("    needs: build\n", job("publish-pypi"))


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


Call = tuple[list[str], Path | None, str | None, bool]


def _stub_steps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, calls: list[Call]) -> None:
    """Every step main() takes before the secret scan stubbed, each kept in ``calls`` instead of run, in order.

    A command `_run` is given is kept with its directory, the PYTHONPATH it runs with (None: none) and whether any
    AUTO_BROWSER_* name reaches it, never its whole environment, so a failing assertion cannot print the environment's
    values; the launch-file check, `_require` and the wheel inspections are kept by name.
    """

    def record(command: list[object], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        seen = os.environ if env is None else env
        reached = any(name.startswith("AUTO_BROWSER_") for name in seen)
        calls.append(([str(part) for part in command], cwd, seen.get("PYTHONPATH"), reached))

    def step(name: str) -> None:
        calls.append(([name], None, None, False))

    monkeypatch.setattr(release_audit, "_run", record)
    monkeypatch.setattr(release_audit, "_check_launch_files", lambda: step("launch files"))
    monkeypatch.setattr(release_audit, "_require", lambda command: step(f"require {command}"))
    for name in ("_inspect_controller_wheel", "_inspect_mcp_wheel", "_inspect_licenses"):
        monkeypatch.setattr(release_audit, name, lambda wheelhouse, name=name: step(name))
    monkeypatch.setattr(release_audit.os, "chdir", lambda path: None)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    # The advisory check runs through `_run`, so it is kept, not run. The doctor needs bash, which main() looks for.
    monkeypatch.setattr(sys, "argv", ["release_audit.py", "--skip-doctor"])


def _audit_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Call]:
    """The steps main() takes, in order, every one stubbed (_stub_steps), the secret scan last, which finds nothing.

    This process's environment holds a made-up PYTHONPATH and AUTO_BROWSER_BASE_URL meanwhile, as a developer's can.
    """
    calls: list[Call] = []
    monkeypatch.setenv("PYTHONPATH", "made-up-host-pythonpath")
    monkeypatch.setenv("AUTO_BROWSER_BASE_URL", "http://made-up-remote.invalid:9000/mcp")
    _stub_steps(monkeypatch, tmp_path, calls)

    def secret_scan(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        calls.append((["git grep"], None, None, False))
        return subprocess.CompletedProcess(args, 1, b"", b"")

    # The secret scan is the one subprocess main() starts itself.
    monkeypatch.setattr(release_audit.subprocess, "run", secret_scan)
    assert release_audit.main() == 0
    return calls


def _step(command: list[str], cwd: Path | None) -> str:
    """What a kept call runs: the module or script an interpreter runs, else its first word; and where, if set."""
    if command[1:2] == ["-m"]:
        name = command[2]
    elif command[1:2] and command[1].endswith(".py"):
        name = Path(command[1]).name
    else:
        name = command[0]
    return f"{name} {cwd.relative_to(release_audit.ROOT).as_posix()}" if cwd else name


def test_main_runs_every_step_once_in_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The audit's steps, in order: a step main() dropped, doubled or moved passes every stubbed test but this one."""
    assert [_step(command, cwd) for command, cwd, *_ in _audit_commands(monkeypatch, tmp_path)] == [
        "launch files", "require git", "require npm", "require uv",
        "check_open_advisories.py",
        "ruff", "check_version_parity.py", "check_playwright_pins.py", "check_bridge_parity.py",
        "agent_eval.py", "fixture_eval.py", "pip_audit", "npm browser-node",
        "build", "build", "build", "build", "_inspect_controller_wheel", "_inspect_mcp_wheel", "_inspect_licenses",
        "pytest controller", "venv", "uv", "pytest client", "venv", "uv", "pytest integrations/langchain",
        "compileall", "git grep",
    ]  # fmt: skip


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
    suites = [i for i, (command, cwd, *_) in enumerate(calls) if cwd == release_audit.ROOT / directory]
    assert len(suites) == 1, f"the audit runs no single suite in {directory}: {calls}"
    suite_command, _, suite_pythonpath, auto_browser_setting = calls[suites[0]]
    assert suite_command[1:-2] == expected
    assert suite_command[-2] == "--basetemp" and Path(suite_command[-1]).is_relative_to(tmp_path)
    # Neither the host's PYTHONPATH nor an AUTO_BROWSER_* setting, which the client reads as its defaults: CI's job
    # sets neither, and a client test that failed on a developer's printed its value.
    assert (suite_pythonpath, auto_browser_setting) == (pythonpath, False)
    interpreter = Path(suite_command[0])

    # The suite's interpreter is a fresh environment the audit made, with the package installed in it as CI installs
    # it: editable, with its extras, and nothing newer than the hold.
    installs = [i for i, (command, *_) in enumerate(calls) if f"./{directory}[{extras}]" in command]
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
        for i, (command, *_) in enumerate(calls)
        if command[1:3] == ["-m", "venv"] and interpreter.is_relative_to(Path(command[-1]))
    ]
    assert len(venvs) == 1 and venvs[0] < installs[0], calls


@pytest.mark.parametrize("status", [1, 3])  # 1: what pytest, ruff, pip-audit, npm audit and the parity checks exit with
def test_a_failing_step_says_which_command_failed_and_where(tmp_path: Path, status: int) -> None:
    with pytest.raises(SystemExit) as caught:
        release_audit._run([sys.executable, "-c", f"raise SystemExit({status})"], cwd=tmp_path)
    message = str(caught.value.code)
    assert message.startswith("Release audit failed:")
    assert f"-c raise SystemExit({status})" in message
    assert str(tmp_path) in message
    assert f"exited with status {status}" in message


# Built at run time, so this file holds no secret-shaped token for the scan itself to find.
SECRET_SHAPES = {
    "an Anthropic key": "sk-ant-api03-" + "Ab1_-" * 8,
    "an OpenAI project key": "sk-proj-" + "Ab1_-" * 8,
    "an AWS access key ID": "AKIA" + "Q7" * 8,
    "an AWS temporary access key ID": "ASIA" + "7Q" * 8,
    "a GitLab token": "glpat-" + "x_-" * 7,
    "a GitHub OAuth token": "gho_" + "a1" * 18,
    "a GitHub server token": "ghs_" + "a1" * 18,
    "a GitHub user-to-server token": "ghu_" + "a1" * 18,
    "a GitHub refresh token": "ghr_" + "a1" * 18,
    "an OpenAI key": "sk-" + "a1" * 12,
    "a GitHub classic token": "ghp_" + "a1" * 18,
}
PROSE = [
    "ask-anthropic-about-it is a branch name",
    "a task-oriented guide to sk-ant- prefixes",
    "AKIA is how an AWS access key ID begins",
    "glpat- and gho_ are token prefixes",
    "AKIAlowercase0123456789",
]


@pytest.mark.parametrize("name", sorted(SECRET_SHAPES))
def test_the_secret_pattern_finds_each_token_shape(name: str) -> None:
    assert re.search(release_audit.SECRET_PATTERN, f"key = {SECRET_SHAPES[name]}\n"), name


@pytest.mark.parametrize("text", PROSE)
def test_the_secret_pattern_passes_prose(text: str) -> None:
    assert re.search(release_audit.SECRET_PATTERN, text) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_grep_reads_the_secret_pattern_as_an_extended_expression(tmp_path: Path) -> None:
    """The scan runs the pattern through `git grep -E`, a POSIX extended expression, not Python's `re`."""
    lines = [*SECRET_SHAPES.values(), *PROSE]
    (tmp_path / "samples.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = subprocess.run(
        [shutil.which("git"), "grep", "--no-index", "-nE", release_audit.SECRET_PATTERN, "--", "samples.txt"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    found = sorted(int(line.split(":", 2)[1]) for line in result.stdout.splitlines())
    assert found == list(range(1, len(SECRET_SHAPES) + 1))


def test_aws_documented_example_key_is_the_one_token_the_scan_lets_through() -> None:
    # The fixture as the PII scrubber's tests hold it, so a change to it shows here. Not written out in this file,
    # which would then hold a token for the scan to find.
    fixture = (Path(__file__).resolve().parent / "test_pii_scrub.py").read_text(encoding="utf-8")
    examples = {match.group(0) for match in re.finditer(release_audit.SECRET_PATTERN, fixture)}
    assert len(examples) == 1, "the PII scrubber's tests hold one secret-shaped token, AWS's example key ID"
    example = examples.pop()
    real = "AKIA" + "Q7" * 8
    assert release_audit._secret_lines(f"CHANGELOG.md:1:  `{example}` passed\n") == []
    assert release_audit._secret_lines(f"a.py:2:key={real}\n") == [f"a.py:2:key={real}"]
    assert release_audit._secret_lines(f"a.py:3:{example} {real}\n") == [f"a.py:3:{example} {real}"]
    # A line git found and Python's reading of the pattern does not is kept: the two engines must not disagree quietly.
    assert release_audit._secret_lines("a.py:4:nothing Python finds\n") == ["a.py:4:nothing Python finds"]


def _scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    files: dict[str, bytes],
    run: Callable[..., subprocess.CompletedProcess] | None = None,
) -> tuple[object, str, str]:
    """main()'s exit (what it returns, or the code it exits with) and what it printed, with every step before the
    secret scan stubbed (_stub_steps), on a repository that tracks ``files``; ``run`` stands in for git."""
    repo = tmp_path / "repo"
    (repo / "controller").mkdir(parents=True)
    (repo / "controller" / "constraints.txt").write_text("idna==3.10\n", encoding="utf-8")
    for name, data in files.items():
        (repo / name).write_bytes(data)
    git = shutil.which("git")
    subprocess.run([git, "init", "-q", str(repo)], check=True)
    subprocess.run([git, "-c", "core.autocrlf=false", "-C", str(repo), "add", "--", *files, "controller"], check=True)
    monkeypatch.setattr(release_audit, "ROOT", repo)
    _stub_steps(monkeypatch, tmp_path, [])
    if run is not None:
        monkeypatch.setattr(release_audit.subprocess, "run", run)
    try:
        code = release_audit.main()
    except SystemExit as exited:
        code = exited.code
    out, err = capsys.readouterr()
    return code, out, err


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_secret_scan_says_where_a_token_is_and_prints_neither_it_nor_its_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A key pasted into a tracked file and not committed, which the scan reads all the same, beside another secret on
    # its line. Both are made up, and built here so that this file holds neither.
    token = SECRET_SHAPES["a GitHub classic token"]
    password = "hunter2-" + "not-real"
    settings = f'name = "x"\nconn = "postgres://admin:{password}@db/x?k={token}"\n'

    code, out, err = _scan(monkeypatch, tmp_path, capsys, {"settings.py": settings.encode()})

    # Compared as booleans, so that a failure prints no secret either.
    failed = "potential secret-shaped token found" in str(code)
    printed = [value in out or value in err for value in (token, password)]
    named = "settings.py:2: secret-shaped token (value withheld)" in err.splitlines()
    assert (failed, printed, named) == (True, [False, False], True)


# What a line git prints can hold, and str.splitlines() reads as a line's end; text mode reads \r so too.
LINE_ENDS_INSIDE_A_LINE = ["\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.parametrize("end", LINE_ENDS_INSIDE_A_LINE, ids=ascii)
def test_the_secret_scan_prints_no_piece_of_a_line_that_holds_what_python_reads_as_a_line_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], end: str
) -> None:
    token = SECRET_SHAPES["a GitHub classic token"]

    code, out, err = _scan(monkeypatch, tmp_path, capsys, {"settings.py": f"x{end}password: {token}\n".encode()})

    failed = "potential secret-shaped token found" in str(code)
    named = err.splitlines() == ["settings.py:1: secret-shaped token (value withheld)"]
    assert (failed, token in out or token in err, named) == (True, False, True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_path_that_holds_a_token_is_withheld_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # git names a text file's match by its path and line, and a binary file's in a line of its own that holds the path.
    token, other = SECRET_SHAPES["a GitHub classic token"], SECRET_SHAPES["a GitHub OAuth token"]
    files = {f"notes-{token}.txt": f"key = {other}\n".encode(), f"keys-{token}.bin": b"\0key = " + other.encode()}

    code, out, err = _scan(monkeypatch, tmp_path, capsys, files)

    failed = "potential secret-shaped token found" in str(code)
    printed = any(value in out or value in err for value in (token, other))
    named = sorted(err.splitlines()) == [
        "(a path that holds one):1: secret-shaped token (value withheld)",
        "a match git names no line of: secret-shaped token (value withheld)",
    ]
    assert (failed, printed, named) == (True, False, True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_failed_scan_prints_not_what_git_said_but_its_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = SECRET_SHAPES["a GitHub classic token"]

    def failed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        # What git says of a GIT_DIR that is no repository: it names the directory, which a setting gave.
        return subprocess.CompletedProcess(command, 128, b"", f"fatal: not a git repository: '/src/{token}'\n".encode())

    code, out, err = _scan(monkeypatch, tmp_path, capsys, {}, run=failed)

    assert (code, token in out or token in err, "git grep exited with status 128" in err) == (128, False, True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.parametrize("encoding", ["utf-8", "latin-1"])
def test_the_secret_scan_reads_what_git_prints_as_utf_8_whatever_the_code_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], encoding: str
) -> None:
    # Read in Windows' ANSI code page (cp1252), the 0x9D that ends a UTF-8 ” stopped the read, and in UTF-8 mode a
    # byte that is not UTF-8 did: a tree that held the one token let through, beside such a byte, failed the audit
    # with a traceback.
    fixture = (Path(__file__).resolve().parent / "test_pii_scrub.py").read_text(encoding="utf-8")
    (example,) = {match.group(0) for match in re.finditer(release_audit.SECRET_PATTERN, fixture)}
    text = {"utf-8": "AWS’s “example” key ID", "latin-1": "la clé d'exemple d'AWS"}[encoding]
    changelog = f"  `{example}` is {text}\n".encode(encoding)

    code, out, err = _scan(monkeypatch, tmp_path, capsys, {"CHANGELOG.md": changelog})

    assert (code, "Release audit passed." in out, example in out or example in err) == (0, True, False)


def test_a_match_is_placed_by_its_path_and_line_alone() -> None:
    token = SECRET_SHAPES["a GitHub classic token"]
    lines = ["a:b.py:4:x = 1", f"notes-{token}.txt:2:x", "Binary file a.bin matches"]
    places = ["a:b.py:4", "(a path that holds one):2", "a match git names no line of"]
    assert [release_audit._secret_place(line) for line in lines] == places


ROOT_LICENSE = _REPO / "LICENSE"


@pytest.mark.skipif(
    not (ROOT_LICENSE.is_file() and RELEASE_WORKFLOW.is_file()), reason="the repository root is not in this tree"
)
def test_each_published_package_holds_the_repository_license() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    published = re.findall(r"python -m build --outdir dist (\S+)", workflow)
    assert len(published) == 3, published
    # The wheels the audit checks for LICENSE are the ones publish-pypi uploads.
    assert re.findall(r"dist_glob: (\S+)-\*", workflow) == list(release_audit.LICENSED_WHEELS)
    expected = ROOT_LICENSE.read_text(encoding="utf-8")
    for directory in published:
        package_license = _REPO / directory / "LICENSE"
        assert package_license.is_file(), f"{directory} has no LICENSE, so its wheel and sdist carry none"
        assert package_license.read_text(encoding="utf-8") == expected, f"{directory}/LICENSE is not the root's"


LICENSE_TEXT = "MIT License\n\nCopyright (c) 2026 Example\n"
LICENSED_WHEELS = ("auto_browser_client", "auto_browser_langchain", "auto_browser_mcp")


def _inspect_licenses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, licenses: dict[str, str | None]) -> None:
    """Wheels named as release.yml's builds name them, each with the given LICENSE text (None: no LICENSE)."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "LICENSE").write_text(LICENSE_TEXT, encoding="utf-8")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for name, text in licenses.items():
        with zipfile.ZipFile(wheelhouse / f"{name}-1.0.0-py3-none-any.whl", "w") as archive:
            archive.writestr(f"{name}-1.0.0.dist-info/METADATA", f"Name: {name}\nLicense-File: LICENSE\n")
            if text is not None:
                archive.writestr(f"{name}-1.0.0.dist-info/licenses/LICENSE", text)
    monkeypatch.setattr(release_audit, "ROOT", root)
    release_audit._inspect_licenses(wheelhouse)


def test_the_audit_accepts_wheels_that_carry_the_license(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    licenses = dict.fromkeys(LICENSED_WHEELS, LICENSE_TEXT)
    licenses["auto_browser_mcp"] = LICENSE_TEXT.replace("\n", "\r\n")  # built from a Windows checkout
    _inspect_licenses(monkeypatch, tmp_path, licenses)


@pytest.mark.parametrize("name", LICENSED_WHEELS)
def test_the_audit_rejects_a_wheel_without_the_license(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    licenses = dict.fromkeys(LICENSED_WHEELS, LICENSE_TEXT) | {name: None}
    with pytest.raises(SystemExit, match=f"{name}-1.0.0-py3-none-any.whl does not carry LICENSE"):
        _inspect_licenses(monkeypatch, tmp_path, licenses)


def test_the_audit_rejects_a_wheel_whose_license_is_another(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    licenses = dict.fromkeys(LICENSED_WHEELS, LICENSE_TEXT) | {"auto_browser_langchain": "Apache License 2.0\n"}
    with pytest.raises(SystemExit, match="auto_browser_langchain-1.0.0-py3-none-any.whl's LICENSE is not"):
        _inspect_licenses(monkeypatch, tmp_path, licenses)


LOCK = """\
pyee==13.0.1 ; python_full_version < '3.12'
    # via playwright
pyee==14.0.0 ; python_full_version >= '3.12'
    # via playwright
pywin32==312 ; sys_platform == 'win32'
colorama==0.4.6 ; os_name == 'nt' or sys_platform == 'win32'
uvloop==0.23.0 ; platform_python_implementation != 'PyPy' and sys_platform != 'cygwin' and sys_platform != 'win32'
async-timeout==5.0.1 ; python_full_version < '3.11.3'
uvicorn[standard]==0.40.0
"""


def test_the_lock_is_read_for_the_image_whatever_the_host() -> None:
    # The image is Linux and CPython 3.11: pyee 13, uvloop, and nothing Windows-only, on this host or any other.
    assert release_audit._image_pins(LOCK) == ["pyee==13.0.1", "uvloop==0.23.0", "uvicorn[standard]==0.40.0"]


def test_the_audit_hands_pip_audit_the_image_pins_without_markers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pip-audit reads a marker for the host it runs on, so the file it is given must hold none."""
    audited: list[str] = []
    calls = []

    def record(command: list[object], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        command = [str(part) for part in command]
        calls.append(command)
        if command[1:3] == ["-m", "pip_audit"]:
            # main() runs from the repository's root, which the stub of os.chdir leaves this process outside.
            audited.append((release_audit.ROOT / command[command.index("-r") + 1]).read_text(encoding="utf-8"))

    _audit_commands(monkeypatch, tmp_path)  # every other step stubbed as there
    monkeypatch.setattr(release_audit, "_run", record)
    assert release_audit.main() == 0

    assert len(audited) == 1, calls
    markers = [line for line in audited[0].splitlines() if ";" in line]
    assert markers == [], "the audited file still holds markers"
    lock = (release_audit.ROOT / "controller" / "constraints.txt").read_text(encoding="utf-8")
    assert audited[0].splitlines() == release_audit._image_pins(lock)
    assert [command for command in calls if command[1:3] == ["-m", "pip_audit"]][0][-2:] == [
        "--disable-pip",
        "--no-deps",
    ]


def test_the_audit_rejects_a_missing_wheel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    licenses = {name: LICENSE_TEXT for name in LICENSED_WHEELS if name != "auto_browser_client"}
    with pytest.raises(SystemExit, match="auto_browser_client wheel was not built"):
        _inspect_licenses(monkeypatch, tmp_path, licenses)


if __name__ == "__main__":
    unittest.main()
