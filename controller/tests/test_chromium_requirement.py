"""AUTO_BROWSER_REQUIRE_CHROMIUM turns "no Chromium" from a skip into a failure, and the CI
workflow runs the real-browser tests where it is set.

`requires_chromium` only skips, so a CI job that installs Chromium could lose it (a failed
install, a moved browser root) and stay green while the real-browser test files skipped.

The helper tests pass the executable in explicitly and never start a browser. The module-level
wiring runs once, when a process first imports `tests._chromium`, so the tests of it start a
child process with the switch and AUTO_BROWSER_TEST_CHROMIUM set as each case needs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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

# The two steps' run text as it must be, compared whole: a list of words misses quoting, line breaks and a # inside a
# word, and each changes what sh runs.
RUN_THE_SHARD = (
    "cd controller\n"
    'python -m pytest tests/ --real-browser-shard "$(( ${{ strategy.job-index }} + 1 ))/${{ strategy.job-total }}" -v'
)
JUDGE_THE_LEGS = (
    'gh api "repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/attempts/$GITHUB_RUN_ATTEMPT/jobs?per_page=100" \\\n'
    '  | python3 scripts/check_real_browser_legs.py "$LEGS_RESULT"'
)


def _jobs() -> dict[str, dict]:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _step(job: dict, runs: str) -> dict:
    """The one step of `job` whose run string holds `runs`."""
    steps = [step for step in job["steps"] if runs in step.get("run", "")]
    assert len(steps) == 1, f"{len(steps)} steps run {runs!r}"
    return steps[0]


@needs_the_workflow
def test_host_tests_run_the_suite_without_a_browser() -> None:
    job = _jobs()["host-tests"]

    assert job["strategy"]["matrix"] == {"python-version": ["3.11", "3.14"]}
    for scope in (job, *job["steps"]):
        assert "AUTO_BROWSER_REQUIRE_CHROMIUM" not in scope.get("env", {}), scope
    for step in job["steps"]:
        assert "playwright install" not in step.get("run", ""), step
        assert "AUTO_BROWSER_REQUIRE_CHROMIUM" not in step.get("run", ""), step


@needs_the_workflow
def test_ci_keeps_the_required_check_names_of_host_tests() -> None:
    """Branch protection requires "host-tests (3.11)" and "host-tests (3.14)"."""
    assert _jobs()["host-tests"]["name"] == "host-tests (${{ matrix.python-version }})"


@needs_the_workflow
def test_each_real_browser_leg_runs_the_shard_of_its_position() -> None:
    """The legs' list names them, through the job's name, so it must be 1 to N for the names to be true; the gate reads
    the legs by that name."""
    job = _jobs()["real-browser-tests"]
    legs = job["strategy"]["matrix"]

    assert job["name"] == "real-browser-tests (${{ matrix.shard }}/${{ strategy.job-total }})"
    assert list(legs) == ["shard"] and legs["shard"] == list(range(1, len(legs["shard"]) + 1)) != []
    assert _step(job, "--real-browser-shard")["run"].strip() == RUN_THE_SHARD


@needs_the_workflow
def test_every_real_browser_leg_installs_chromium_and_requires_it() -> None:
    """The job and its test step hold only the keys they have, so no env, defaults, container or continue-on-error
    (a PYTEST_ADDOPTS of --collect-only, say) changes how the shard runs unseen."""
    jobs = _jobs()
    job = jobs["real-browser-tests"]
    install = _step(job, "playwright install")
    test = _step(job, "--real-browser-shard")

    assert set(job) == {"name", "timeout-minutes", "needs", "runs-on", "strategy", "steps"}
    assert set(test) == {"name", "env", "run"} and test["env"] == {"AUTO_BROWSER_REQUIRE_CHROMIUM": "1"}
    for step in job["steps"]:
        assert not {"if", "continue-on-error"} & set(step), step
    assert job["steps"].index(install) < job["steps"].index(test)
    assert install["run"].strip() == "python -m playwright install --with-deps chromium"
    assert _step(job, "uv pip install")["run"] == _step(jobs["host-tests"], "uv pip install")["run"]


@needs_the_workflow
def test_one_check_passes_only_when_the_script_passes_every_leg() -> None:
    """The judging step is that one pipeline and nothing else; bash by name adds pipefail, so a failed API call fails
    it. The job and the step hold only the keys they have. scripts/check_real_browser_legs.py's own tests hold what it
    passes."""
    job = _jobs()["real-browser-tests-done"]
    needs = job["needs"]
    steps = job["steps"]
    judging = _step(job, "check_real_browser_legs.py")
    checkout = next((step for step in steps if step.get("uses", "").startswith("actions/checkout@")), None)

    assert set(job) == {"if", "needs", "runs-on", "permissions", "steps"} and job["if"] == "always()"
    assert "real-browser-tests" in ([needs] if isinstance(needs, str) else needs)
    assert job["permissions"] == {"actions": "read", "contents": "read"}
    for step in steps:
        assert not {"if", "continue-on-error"} & set(step), step
    assert checkout is not None and steps.index(checkout) < steps.index(judging)
    assert (checkout.get("with") or {}).get("persist-credentials") is False
    assert set(judging) == {"name", "shell", "env", "run"} and judging["shell"] == "bash"
    assert judging["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "LEGS_RESULT": "${{ needs.real-browser-tests.result }}",
    }
    assert judging["run"].strip() == JUDGE_THE_LEGS


@needs_the_workflow
def test_the_workflow_sets_only_its_one_env_variable_and_no_defaults() -> None:
    """A workflow's env and defaults reach every job's steps, the shard and judging steps among them."""
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))

    assert workflow["env"] == {"FORCE_JAVASCRIPT_ACTIONS_TO_NODE24": True}
    assert "defaults" not in workflow


@needs_the_workflow
def test_compose_smoke_needs_the_real_browser_tests_and_is_the_last_job() -> None:
    """Four tests read ci.yml from `  compose-smoke:` to the end of the file as that one job: two in
    test_browser_sandbox.py, one in test_round5_sandbox.py and one in test_run_as.py."""
    jobs = _jobs()

    assert "real-browser-tests" in jobs["compose-smoke"]["needs"]
    assert list(jobs)[-1] == "compose-smoke"
