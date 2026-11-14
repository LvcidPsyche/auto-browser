#!/usr/bin/env python
from __future__ import annotations

import argparse
import configparser
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
LAUNCH_FILES = [
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "ROADMAP.md",
    "TIPS.md",
    "docs/agent-evals.md",
    # docs/launch.md was retired in 6a5f705 as launch-era material pinned to
    # v1.2.1. This list was not updated, so the audit has failed at its first
    # check ever since — which is also how we know it had not been run.
    "docs/mcp-clients.md",
    "docs/good-first-issues.md",
    "docs/assets/hero.svg",
    "examples/README.md",
    "examples/claude_desktop_config.json",
    "scripts/compose_local.sh",
    "scripts/doctor.sh",
    "scripts/mcp_stdio_bridge.py",
]
# Every directory release.yml builds, plus the controller, whose wheel is built only to be inspected (not published:
# see controller/pyproject.toml).
WHEEL_PACKAGES = ("controller", "client", "integrations/langchain", "packaging/auto-browser-mcp")
MCP_CONSOLE_SCRIPT = "auto_browser_client.mcp_bridge:main"
# The wheels release.yml publishes: each must carry the repository's LICENSE, which its backend takes from the package
# directory's own copy (PEP 639's license files; setuptools and hatchling both look for LICEN[CS]E* by default).
LICENSED_WHEELS = ("auto_browser_client", "auto_browser_langchain", "auto_browser_mcp")
# The client's and the LangChain integration's suites, as ci.yml's client-tests and langchain-tests run them:
# (directory, extras, coverage module, coverage gate, PYTHONPATH). Each runs in a fresh environment of its own, where
# its package is installed editable with those extras, from releases at least PACKAGE_HOLD_HOURS old, as in CI.
PACKAGE_SUITES = (
    ("client", "dev", "auto_browser_client", 85, "."),
    ("integrations/langchain", "langchain,dev", "auto_browser_langchain", 90, None),
)
PACKAGE_HOLD_HOURS = 72
# A POSIX extended expression, which `git grep -E` reads (no \b, no lookaround).
SECRET_PATTERN = (
    r"sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}|ghp_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"sk-ant-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|(AKIA|ASIA)[0-9A-Z]{16}|"
    r"glpat-[A-Za-z0-9_-]{20}|gh[osur]_[A-Za-z0-9]{20,}"
)
# Published examples, not credentials, each let through only as itself and named by its SHA-256, so this file holds no
# token for this scan or another to find: AWS's documented example access key ID, which the PII scrubber's tests scrub
# and the CHANGELOG quotes.
SECRET_EXAMPLE_SHA256 = frozenset({"1a5d44a2dca19669d72edf4c4f1c27c4c1ca4b4408fbb17f6ce4ad452d78ddb3"})
# The platform the controller image runs, for which the lock's markers are read: python:3.11-slim, CPython 3.11 on
# Linux x86_64. The image takes the newest 3.11 release, 3.11.15 when this was written.
IMAGE_MARKER_ENVIRONMENT = {
    "implementation_name": "cpython",
    "implementation_version": "3.11.15",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.11.15",
    "python_version": "3.11",
    "sys_platform": "linux",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the auto-browser release audit.")
    parser.add_argument("--skip-doctor", action="store_true", help="Skip the bash-based readiness doctor.")
    parser.add_argument(
        "--skip-advisory-check",
        action="store_true",
        help="Skip the unacknowledged-advisory gate (offline dry-runs only).",
    )
    args = parser.parse_args()

    os.chdir(ROOT)
    _check_launch_files()
    _require("git")
    _require("npm")
    _require("uv")

    # Early, because it is the cheapest possible failure and the one most likely
    # to mean "do not ship yet".
    if args.skip_advisory_check:
        print("Skipping the security-advisory triage check by request.")
    else:
        print("Checking for security advisories waiting in triage...")
        _run([PYTHON, "scripts/check_open_advisories.py"])

    print("Running lint...")
    # Repo-wide, like `make lint`: a path list here skipped client/ and
    # integrations/, two of the three packages a release publishes.
    _run([PYTHON, "-m", "ruff", "check", "."])

    print("Checking version, Playwright-pin and bridge parity...")
    for check in ("check_version_parity.py", "check_playwright_pins.py", "check_bridge_parity.py"):
        _run([PYTHON, f"scripts/{check}"])

    print("Running deterministic agent eval scoring...")
    _run([PYTHON, "scripts/agent_eval.py", "--mock"])

    print("Running fixture eval validation...")
    _run([PYTHON, "scripts/fixture_eval.py"])

    print("Running Python dependency audit...")
    # The tree controller/constraints.txt pins, read as it is (R5C-14): a resolve would audit other versions, and first
    # installs pip, wheel and setuptools unpinned. pip-audit reads each line's markers for the host it runs on, so it
    # is given the lines the image installs (IMAGE_MARKER_ENVIRONMENT), without their markers: from any host it audits
    # what CI's dependency-audit job, on Linux and Python 3.11, audits.
    lock = (ROOT / "controller" / "constraints.txt").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="auto-browser-pins-") as temp_dir:
        pins = Path(temp_dir) / "image-pins.txt"
        pins.write_text("\n".join(_image_pins(lock)) + "\n", encoding="utf-8")
        _run([PYTHON, "-m", "pip_audit", "-r", pins, "--disable-pip", "--no-deps"])

    print("Running browser-node production dependency audit...")
    _run(["npm", "audit", "--omit=dev", "--audit-level=high"], cwd=ROOT / "browser-node")

    print("Building and inspecting Python wheels...")
    with tempfile.TemporaryDirectory(prefix="auto-browser-wheelhouse-") as temp_dir:
        wheelhouse = Path(temp_dir)
        for package_dir in WHEEL_PACKAGES:
            _run([PYTHON, "-m", "build", "--wheel", "--outdir", str(wheelhouse), package_dir])
        _inspect_controller_wheel(wheelhouse)
        _inspect_mcp_wheel(wheelhouse)
        _inspect_licenses(wheelhouse)

    print("Running controller tests with coverage gate...")
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    _run(
        [
            PYTHON,
            "-m",
            "pytest",
            "tests",
            "--cov=app",
            "--cov-report=term",
            "--cov-fail-under=80",
            "-q",
            "--basetemp",
            str(ROOT / ".pytest-tmp-release-audit"),
        ],
        cwd=ROOT / "controller",
        env=env,
    )

    for directory, extras, module, gate, pythonpath in PACKAGE_SUITES:
        print(f"Running {directory} tests with coverage gate...")
        _run_package_suite(directory, extras, module, gate, pythonpath)

    print("Running compile checks...")
    _run([PYTHON, "-m", "compileall", "-q", "controller/app", "client/auto_browser_client", "integrations/langchain"])

    if args.skip_doctor:
        print("Skipping doctor by request.")
    else:
        bash = shutil.which("bash")
        if not bash:
            raise SystemExit("Missing required command: bash. Use --skip-doctor only for local Windows dry-runs.")
        print("Running readiness doctor...")
        env = os.environ.copy()
        env["SMOKE_PROVIDER"] = "disabled"
        env["DOCTOR_BUILD"] = "1"
        _run([bash, "scripts/doctor.sh"], env=env)

    print("Scanning tracked files for obvious secret-shaped tokens...")
    secret_scan = subprocess.run(
        ["git", "grep", "-nE", SECRET_PATTERN, "--", "."],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if secret_scan.returncode not in {0, 1}:
        print(secret_scan.stderr, file=sys.stderr)
        raise SystemExit(secret_scan.returncode)
    found = _secret_lines(secret_scan.stdout)
    if found:
        print("\n".join(found), file=sys.stderr)
        raise SystemExit("Release audit failed: potential secret-shaped token found.")

    print()
    print("Release audit passed.")
    return 0


def _image_pins(lock: str) -> list[str]:
    """The lock's requirements whose markers hold for the image, each written without its marker."""
    pins = []
    for raw in lock.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        if requirement.marker is None or requirement.marker.evaluate(IMAGE_MARKER_ENVIRONMENT):
            requirement.marker = None
            pins.append(str(requirement))
    return pins


def _secret_lines(grep_output: str) -> list[str]:
    """The lines `git grep` found that hold a secret-shaped token other than a published example.

    A line is let through only when Python's reading of the pattern finds a token in it and every token is an example,
    so a line the two engines read differently stays found.
    """
    found = []
    for line in grep_output.splitlines():
        digests = [hashlib.sha256(match.group(0).encode()).hexdigest() for match in re.finditer(SECRET_PATTERN, line)]
        if not digests or any(digest not in SECRET_EXAMPLE_SHA256 for digest in digests):
            found.append(line)
    return found


def _check_launch_files() -> None:
    print("Checking launch-critical files...")
    missing = [path for path in LAUNCH_FILES if not (ROOT / path).is_file()]
    if missing:
        raise SystemExit("Missing required release file(s): " + ", ".join(missing))


def _inspect_controller_wheel(wheelhouse: Path) -> None:
    wheels = sorted(wheelhouse.glob("auto_browser_controller-*.whl"))
    if not wheels:
        raise SystemExit("Controller wheel was not built")
    wheel = wheels[-1]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    forbidden = [name for name in names if name.startswith(("app/social/", "app/integrations/"))]
    if forbidden:
        sample = ", ".join(forbidden[:5])
        raise SystemExit(f"Controller wheel still includes extracted social/Veo3 modules: {sample}")


def _inspect_mcp_wheel(wheelhouse: Path) -> None:
    """The metapackage ships no code: it only depends on the client and re-declares its script."""
    wheels = sorted(wheelhouse.glob("auto_browser_mcp-*.whl"))
    if not wheels:
        raise SystemExit("MCP metapackage wheel was not built")
    with zipfile.ZipFile(wheels[-1]) as archive:
        names = archive.namelist()
        modules = [name for name in names if name.endswith(".py")]
        if modules:
            raise SystemExit("MCP metapackage wheel must not contain Python modules: " + ", ".join(modules[:5]))
        metadata = _read_dist_info(archive, names, "METADATA")
        entry_points = _read_dist_info(archive, names, "entry_points.txt")
    if not any(line.startswith("Requires-Dist: auto-browser-client") for line in metadata.splitlines()):
        raise SystemExit("MCP metapackage wheel is missing Requires-Dist: auto-browser-client")
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # script names are case-sensitive
    parser.read_string(entry_points)
    script = parser.get("console_scripts", "auto-browser-mcp", fallback=None)
    if script != MCP_CONSOLE_SCRIPT:
        raise SystemExit(f"MCP metapackage wheel must map auto-browser-mcp to {MCP_CONSOLE_SCRIPT}, found {script!r}")


def _inspect_licenses(wheelhouse: Path) -> None:
    """Each published wheel carries the repository's LICENSE in its .dist-info/licenses."""
    expected = (ROOT / "LICENSE").read_text(encoding="utf-8")
    for name in LICENSED_WHEELS:
        wheels = sorted(wheelhouse.glob(f"{name}-*.whl"))
        if not wheels:
            raise SystemExit(f"{name} wheel was not built")
        with zipfile.ZipFile(wheels[-1]) as archive:
            licenses = [entry for entry in archive.namelist() if entry.endswith(".dist-info/licenses/LICENSE")]
            if len(licenses) != 1:
                raise SystemExit(f"{wheels[-1].name} does not carry LICENSE in its .dist-info/licenses")
            # A checkout with CRLF line endings builds the same text with them.
            text = archive.read(licenses[0]).decode("utf-8").replace("\r\n", "\n")
        if text != expected:
            raise SystemExit(f"{wheels[-1].name}'s LICENSE is not the repository's LICENSE")


def _read_dist_info(archive: zipfile.ZipFile, names: list[str], filename: str) -> str:
    matches = [name for name in names if name.endswith(f".dist-info/{filename}")]
    return archive.read(matches[0]).decode("utf-8") if matches else ""


def _run_package_suite(directory: str, extras: str, module: str, gate: int, pythonpath: str | None) -> None:
    """Run one package's tests in an environment of its own, as CI does.

    The controller's environment holds what the package's tests need and much more, so a dependency the package uses
    without declaring it would pass here and fail in CI and for its users.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=PACKAGE_HOLD_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with tempfile.TemporaryDirectory(prefix="auto-browser-suite-") as temp_dir:
        venv = Path(temp_dir) / "venv"
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        _run([PYTHON, "-m", "venv", "--without-pip", venv])
        _run(["uv", "pip", "install", "--python", python, "--exclude-newer", cutoff, "-e", f"./{directory}[{extras}]"])
        env = None
        if pythonpath is not None:
            env = os.environ.copy()
            env["PYTHONPATH"] = pythonpath
        _run(
            [
                python,
                "-m",
                "pytest",
                "tests",
                "-q",
                f"--cov={module}",
                "--cov-report=term-missing",
                f"--cov-fail-under={gate}",
                # As the controller's step above: pytest's shared default under the system temp directory can be
                # unusable on Windows.
                "--basetemp",
                Path(temp_dir) / "basetemp",
            ],
            cwd=ROOT / directory,
            env=env,
        )


def _require(command: str) -> None:
    if shutil.which(command) is None:
        raise SystemExit(f"Missing required command: {command}")


def _run(command: list[object], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    resolved = [str(part) for part in command]
    executable = shutil.which(resolved[0])
    if executable:
        resolved[0] = executable
    printable = " ".join(resolved)
    print(f"$ {printable}")
    directory = cwd or ROOT
    result = subprocess.run(resolved, cwd=directory, env=env)
    if result.returncode != 0:
        raise SystemExit(f"Release audit failed: `{printable}` in {directory} exited with status {result.returncode}.")


if __name__ == "__main__":
    raise SystemExit(main())
