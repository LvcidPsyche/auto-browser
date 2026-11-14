#!/usr/bin/env python
from __future__ import annotations

import argparse
import configparser
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
# Every directory release.yml builds, plus the controller (not published yet).
WHEEL_PACKAGES = ("controller", "client", "integrations/langchain", "packaging/auto-browser-mcp")
MCP_CONSOLE_SCRIPT = "auto_browser_client.mcp_bridge:main"
# The client's and the LangChain integration's suites, as ci.yml's client-tests and langchain-tests run them:
# (directory, extras, coverage module, coverage gate, PYTHONPATH). Each runs in a fresh environment of its own, where
# its package is installed editable with those extras, from releases at least PACKAGE_HOLD_HOURS old, as in CI.
PACKAGE_SUITES = (
    ("client", "dev", "auto_browser_client", 85, "."),
    ("integrations/langchain", "langchain,dev", "auto_browser_langchain", 90, None),
)
PACKAGE_HOLD_HOURS = 72
SECRET_PATTERN = (
    r"sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}|ghp_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
)


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
    # installs pip, wheel and setuptools unpinned. pip-audit takes the lines whose markers match this host's platform
    # and Python; CI's dependency-audit job reads the file on Linux and Python 3.11, as the image is.
    _run([PYTHON, "-m", "pip_audit", "-r", "controller/constraints.txt", "--disable-pip", "--no-deps"])

    print("Running browser-node production dependency audit...")
    _run(["npm", "audit", "--omit=dev", "--audit-level=high"], cwd=ROOT / "browser-node")

    print("Building and inspecting Python wheels...")
    with tempfile.TemporaryDirectory(prefix="auto-browser-wheelhouse-") as temp_dir:
        wheelhouse = Path(temp_dir)
        for package_dir in WHEEL_PACKAGES:
            _run([PYTHON, "-m", "build", "--wheel", "--outdir", str(wheelhouse), package_dir])
        _inspect_controller_wheel(wheelhouse)
        _inspect_mcp_wheel(wheelhouse)

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
    if secret_scan.returncode == 0:
        print(secret_scan.stdout, file=sys.stderr)
        raise SystemExit("Release audit failed: potential secret-shaped token found.")
    if secret_scan.returncode not in {0, 1}:
        print(secret_scan.stderr, file=sys.stderr)
        raise SystemExit(secret_scan.returncode)

    print()
    print("Release audit passed.")
    return 0


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
