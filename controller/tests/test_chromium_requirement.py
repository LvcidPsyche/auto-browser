"""AUTO_BROWSER_REQUIRE_CHROMIUM turns "no Chromium" from a skip into a failure.

`requires_chromium` only skips, so a CI job that installs Chromium could lose it (a failed
install, a moved browser root) and stay green while the real-browser test files skipped. The
real-browser-tests job, which installs Chromium, sets the variable, and a missing browser then
fails collection. Under the switch a real-browser test that skips for any other reason fails
the run too (tests/_real_browser_shards.py, tested in test_real_browser_shards.py).

The helper tests pass the executable in explicitly and never start a browser. Importing
`tests._chromium` still runs its one real lookup (it globs the Playwright browser roots), so a
test that imports it runs in a child process with AUTO_BROWSER_TEST_CHROMIUM set, which makes
that lookup answer from the variable alone.
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


def _pytest_on(tree: Path, *options: str) -> list[str]:
    """A pytest child's arguments to run the tests in `tree`. The pytest.ini written there makes the tree its own
    rootdir: without one the rootdir was the common ancestor of the controller and the temp folder (the home folder on
    a developer machine), and collection walked every folder in between, which another process may be removing."""
    (tree / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return ["-m", "pytest", "-p", "no:cacheprovider", *options, str(tree)]


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
    args = _pytest_on(tmp_path, "-q")

    skipped = _child(args, chromium=MISSING, required=None)
    ran = _child(args, chromium=sys.executable, required="1")

    assert skipped.returncode == 0 and "1 skipped" in skipped.stdout, skipped.stdout + skipped.stderr
    assert ran.returncode == 0 and "1 passed" in ran.stdout, ran.stdout + ran.stderr


needs_the_workflow = pytest.mark.skipif(
    not CI_WORKFLOW.is_file(), reason="repo checkout not available (controller image)"
)


def _ci_job(name: str) -> str:
    """The body of one job in ci.yml, up to the next job."""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    job = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z-]*:\n|\Z)", text, flags=re.M | re.S)
    assert job, f"no {name} job in ci.yml"
    return job.group(1)


@needs_the_workflow
def test_host_tests_run_without_a_browser() -> None:
    """The real-browser tests have a job of their own: host-tests installs no Chromium and does not require one, so on
    both of its legs they skip."""
    body = _ci_job("host-tests")

    assert "playwright install" not in body
    assert "AUTO_BROWSER_REQUIRE_CHROMIUM" not in body
    assert "real_browser" not in body


@needs_the_workflow
def test_ci_keeps_the_required_check_names_of_host_tests() -> None:
    """Branch protection requires "host-tests (3.11)" and "host-tests (3.14)": the job names itself from the Python
    version only, since a matrix value added later would join the default name (an `include` once made it
    "host-tests (3.11, true)"). The other matrix job, langchain-tests, keeps its default name, "langchain-tests
    (<version>)"."""
    assert "    name: host-tests (${{ matrix.python-version }})\n" in _ci_job("host-tests")


@needs_the_workflow
def test_the_real_browser_job_installs_chromium_requires_it_and_runs_one_shard() -> None:
    """Chromium is installed with no condition, the switch is on (a missing browser, or a real-browser test that skips,
    fails the shard), and the shard count comes from the matrix, the one place it is written. No coverage gate: one
    shard runs a part of the suite, and the host-tests legs keep the gate."""
    body = _ci_job("real-browser-tests")

    assert "if:" not in body
    assert "run: python -m playwright install --with-deps chromium\n" in body
    switch = re.search(r"AUTO_BROWSER_REQUIRE_CHROMIUM: (.+)\n", body)
    assert switch and switch.group(1).strip() == '"1"'
    assert "cd controller\n" in body
    assert 'python -m pytest tests/ --real-browser-shard "${{ matrix.shard }}/${{ strategy.job-total }}"' in body
    assert "--cov" not in body


@needs_the_workflow
def test_one_check_passes_only_when_every_shard_passed() -> None:
    """Branch protection can require one name however many shards there are. The job runs always: one skipped because
    a job it needs failed reports success, and would pass the check."""
    body = _ci_job("real-browser-tests-done")

    assert re.search(r"^    if: always\(\)\n", body, flags=re.M)
    assert re.search(r"^    needs: real-browser-tests\n", body, flags=re.M)
    assert 'run: test "${{ needs.real-browser-tests.result }}" = success\n' in body


@needs_the_workflow
def test_compose_smoke_needs_the_real_browser_tests_and_the_new_jobs_come_before_it() -> None:
    """compose-smoke needs the real-browser tests as it needs host-tests. The new jobs come before it, since tests read
    ci.yml from `  compose-smoke:` to the end of the file as that one job."""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    needs = re.search(r"^    needs: \[(.*)\]\n", _ci_job("compose-smoke"), flags=re.M)

    assert needs and "real-browser-tests" in [name.strip() for name in needs.group(1).split(",")]
    smoke = text.index("\n  compose-smoke:\n")
    assert text.index("\n  real-browser-tests:\n") < smoke
    assert text.index("\n  real-browser-tests-done:\n") < smoke
