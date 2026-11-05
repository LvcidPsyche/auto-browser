"""AUTO_BROWSER_REQUIRE_CHROMIUM turns "no Chromium" from a skip into a failure.

`requires_chromium` only skips, so a CI leg that installs Chromium could lose it (a failed
install, a moved browser root) and stay green while the real-browser test files skipped. The
leg that installs Chromium sets the variable, and a missing browser then fails collection.

The helper tests pass the executable in explicitly and never start a browser. Importing
`tests._chromium` still runs its one real lookup (it globs the Playwright browser roots), so a
test that imports it runs in a child process with AUTO_BROWSER_TEST_CHROMIUM set, which makes
that lookup answer from the variable alone.

Known limit: the switch checks only that some Chromium resolves. Tests with their own extra
skip conditions (for example the ones that skip when only chrome-headless-shell is found) still
skip silently under it, and nothing counts how many real-browser tests actually ran.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests._chromium import ChromiumRequiredError, chromium_required, requires_chromium_mark

CONTROLLER_DIR = Path(__file__).resolve().parents[1]
CI_WORKFLOW = CONTROLLER_DIR.parent / ".github" / "workflows" / "ci.yml"
MISSING = "/nonexistent/chromium"


def _child(
    args: list[str], *, chromium: str, required: str | None, cwd: Path = CONTROLLER_DIR
) -> subprocess.CompletedProcess[str]:
    """A python child with a minimal environment: the switch and the executable override, nothing inherited."""
    env = {"PATH": os.environ.get("PATH", ""), "AUTO_BROWSER_TEST_CHROMIUM": chromium}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    if required is not None:
        env["AUTO_BROWSER_REQUIRE_CHROMIUM"] = required
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=120, env=env, cwd=cwd)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " 1 "])
def test_the_switch_is_on_for_truthy_values(value: str) -> None:
    assert chromium_required({"AUTO_BROWSER_REQUIRE_CHROMIUM": value}) is True


@pytest.mark.parametrize("environ", [{}, {"AUTO_BROWSER_REQUIRE_CHROMIUM": ""}, {"AUTO_BROWSER_REQUIRE_CHROMIUM": "0"}])
def test_the_switch_is_off_when_unset_empty_or_zero(environ: dict[str, str]) -> None:
    assert chromium_required(environ) is False


def test_without_the_switch_a_missing_chromium_does_not_raise() -> None:
    assert requires_chromium_mark(None, required=False) is not None


def test_with_the_switch_a_missing_chromium_fails() -> None:
    with pytest.raises(ChromiumRequiredError, match="AUTO_BROWSER_REQUIRE_CHROMIUM"):
        requires_chromium_mark(None, required=True)


@pytest.mark.parametrize("required", [False, True])
def test_a_found_chromium_never_fails(required: bool) -> None:
    assert requires_chromium_mark("/some/chrome", required=required) is not None


def test_the_module_level_wiring_fails_import_when_required_and_missing() -> None:
    result = _child(["-c", "import tests._chromium"], chromium=MISSING, required="1")

    assert result.returncode != 0
    assert "ChromiumRequiredError" in result.stderr


def test_the_module_level_wiring_imports_when_not_required() -> None:
    for required in (None, "", "0"):
        result = _child(["-c", "import tests._chromium"], chromium=MISSING, required=required)

        assert result.returncode == 0, result.stderr


def test_a_real_browser_test_skips_without_a_chromium_and_runs_with_one(tmp_path: Path) -> None:
    test_file = tmp_path / "test_probe.py"
    test_file.write_text(
        "from tests._chromium import requires_chromium\n\n\n@requires_chromium\ndef test_probe():\n    pass\n",
        encoding="utf-8",
    )
    args = ["-m", "pytest", "-p", "no:cacheprovider", "-q", str(test_file)]

    skipped = _child(args, chromium=MISSING, required=None)
    ran = _child(args, chromium=sys.executable, required="1")

    assert skipped.returncode == 0 and "1 skipped" in skipped.stdout, skipped.stdout + skipped.stderr
    assert ran.returncode == 0 and "1 passed" in ran.stdout, ran.stdout + ran.stderr


@pytest.mark.skipif(not CI_WORKFLOW.is_file(), reason="repo checkout not available (controller image)")
def test_ci_installs_chromium_on_the_leg_that_requires_it() -> None:
    """One matrix flag drives both the install step and the switch, so they cannot drift apart."""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    job = re.search(r"^  host-tests:\n(.*?)^  [a-z][a-z-]*:\n", text, flags=re.M | re.S)
    assert job, "no host-tests job in ci.yml"
    body = job.group(1)

    assert re.search(r'include:\n(?:\s+#.*\n)*\s+- python-version: "3\.11"\n\s+real_browser: true\n', body)
    install = re.search(r"- name: Install Chromium for the real-browser tests\n\s+if: (.+)\n", body)
    assert install and install.group(1).strip() == "matrix.real_browser"
    switch = re.search(r"AUTO_BROWSER_REQUIRE_CHROMIUM: (.+)\n", body)
    assert switch and switch.group(1).strip() == "${{ matrix.real_browser && '1' || '' }}"
    assert "python-version == " not in body


@pytest.mark.skipif(not CI_WORKFLOW.is_file(), reason="repo checkout not available (controller image)")
def test_ci_keeps_the_required_check_names_of_host_tests() -> None:
    """An `include` value joins the default job name ("host-tests (3.11, true)"), and branch protection requires
    "host-tests (3.11)" and "host-tests (3.14)": the job names itself from the Python version only. (The other
    matrix job, langchain-tests, has no include, so its default name is already "langchain-tests (<version>)".)"""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    job = re.search(r"^  host-tests:\n(.*?)^  [a-z][a-z-]*:\n", text, flags=re.M | re.S)
    assert job, "no host-tests job in ci.yml"
    assert "    name: host-tests (${{ matrix.python-version }})\n" in job.group(1)
