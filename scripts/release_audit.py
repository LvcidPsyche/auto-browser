#!/usr/bin/env python
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
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
    "docs/launch.md",
    "docs/mcp-clients.md",
    "docs/good-first-issues.md",
    "docs/assets/hero.svg",
    "examples/README.md",
    "examples/claude_desktop_config.json",
    "scripts/compose_local.sh",
    "scripts/doctor.sh",
    "scripts/mcp_stdio_bridge.py",
]
SECRET_PATTERN = (
    r"sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}|ghp_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the auto-browser release audit.")
    parser.add_argument("--skip-doctor", action="store_true", help="Skip the bash-based readiness doctor.")
    args = parser.parse_args()

    os.chdir(ROOT)
    _check_launch_files()
    _require("git")
    _require("npm")

    print("Running lint...")
    _run([PYTHON, "-m", "ruff", "check", "controller/app", "controller/tests", "scripts", "--select", "E9,F,I"])

    print("Running deterministic agent eval scoring...")
    _run([PYTHON, "scripts/agent_eval.py", "--mock"])

    print("Running fixture eval validation...")
    _run([PYTHON, "scripts/fixture_eval.py"])

    print("Running Python dependency audit...")
    _run([PYTHON, "-m", "pip_audit", "-r", "controller/requirements.txt"])

    print("Running browser-node production dependency audit...")
    _run(["npm", "audit", "--omit=dev", "--audit-level=high"], cwd=ROOT / "browser-node")

    print("Building and inspecting Python wheels...")
    with tempfile.TemporaryDirectory(prefix="auto-browser-wheelhouse-") as temp_dir:
        wheelhouse = Path(temp_dir)
        for package_dir in ("controller", "client", "integrations/langchain"):
            _run([PYTHON, "-m", "build", "--wheel", "--outdir", str(wheelhouse), package_dir])
        _inspect_controller_wheel(wheelhouse)

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
    subprocess.run(resolved, cwd=cwd or ROOT, env=env, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
