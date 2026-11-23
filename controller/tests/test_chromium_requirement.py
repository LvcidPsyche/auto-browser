"""AUTO_BROWSER_REQUIRE_CHROMIUM turns "no Chromium" from a skip into a failure, and the CI
workflow runs the real-browser tests where it is set.

`requires_chromium` only skips, so a CI job that installs Chromium could lose it (a failed
install, a moved browser root) and stay green while the real-browser test files skipped.

The helper tests pass the executable in explicitly and never start a browser. The module-level
wiring runs once, when a process first imports `tests._chromium`, so the tests of it start a
child process with the switch and AUTO_BROWSER_TEST_CHROMIUM set as each case needs.

The search for a Chromium takes the build the installed pip Playwright drives, not the newest in the cache, which is
another browser as soon as two Playwright versions have been on one machine. Its tests plant browser folders and a
driver's browsers.json, and call the search itself: `chromium_executable()` is cached, and ran at import.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import playwright
import pytest
import yaml

from tests import _chromium
from tests._chromium import ChromiumRequiredError, chromium_required, requires_chromium_mark

CONTROLLER_DIR = Path(__file__).resolve().parents[1]
CI_WORKFLOW = CONTROLLER_DIR.parent / ".github" / "workflows" / "ci.yml"
MISSING = "/nonexistent/chromium"

# The builds a planted driver names, and another build beside them, newer than both.
DRIVER_BUILD = "1100"
SHELL_BUILD = "1090"
NEWER_BUILD = "1200"
# Each binary in the Linux layout: the search looks for every platform's.
CHROME = "chrome-linux64/chrome"
HEADLESS_SHELL = "chrome-headless-shell-linux64/chrome-headless-shell"


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


def _plant(root: Path, folder: str, binary: str = CHROME) -> Path:
    """A browser binary in `folder` (a build's folder, as Playwright names it) under the browser root `root`."""
    path = root / folder / binary
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    return path


def _driver_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str | None) -> None:
    """An installed Playwright whose driver's browsers.json holds `text` (None: it has none). The search reads the file
    where Playwright finds its own driver, beside the package's `__file__`."""
    package = tmp_path / "site-packages" / "playwright"
    browsers = package / "driver" / "package" / "browsers.json"
    browsers.parent.mkdir(parents=True)
    if text is not None:
        browsers.write_text(text, encoding="utf-8")
    monkeypatch.setattr(playwright, "__file__", str(package / "__init__.py"))


def _driver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, chromium: str = DRIVER_BUILD, shell: str = DRIVER_BUILD
) -> None:
    """An installed Playwright whose driver names these builds of Chromium and of its headless shell."""
    entries = [
        {"name": "chromium", "revision": chromium, "browserVersion": "150.0.1.2"},
        {"name": "chromium-headless-shell", "revision": shell, "browserVersion": "150.0.1.2"},
        {"name": "firefox", "revision": "1500", "browserVersion": "149.0"},
    ]
    _driver_file(monkeypatch, tmp_path, json.dumps({"comment": "planted", "browsers": entries}))


def _search(monkeypatch: pytest.MonkeyPatch, *roots: Path) -> Path | None:
    """What the search finds under these browser roots, in this order, with no override."""
    monkeypatch.delenv("AUTO_BROWSER_TEST_CHROMIUM", raising=False)
    monkeypatch.setattr(_chromium, "_browser_roots", lambda: list(roots))
    found = _chromium.chromium_executable.__wrapped__()
    return None if found is None else Path(found)


def test_the_search_takes_the_drivers_build_over_a_newer_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """And Chromium's own binary over the headless shell's: two files tell which they got by "headless-shell" in it."""
    root = tmp_path / "root"
    drivers = _plant(root, f"chromium-{DRIVER_BUILD}")
    _plant(root, f"chromium_headless_shell-{DRIVER_BUILD}", HEADLESS_SHELL)
    _plant(root, f"chromium-{NEWER_BUILD}")
    _driver(monkeypatch, tmp_path)

    assert _search(monkeypatch, root) == drivers


def test_the_search_takes_no_other_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "root"
    _plant(root, f"chromium-{NEWER_BUILD}")
    _plant(root, f"chromium_headless_shell-{NEWER_BUILD}", HEADLESS_SHELL)
    _driver(monkeypatch, tmp_path)

    assert _search(monkeypatch, root) is None


def test_the_search_takes_the_drivers_build_in_a_later_root_over_another_in_an_earlier_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _plant(first, f"chromium-{NEWER_BUILD}")
    drivers = _plant(second, f"chromium-{DRIVER_BUILD}")
    _driver(monkeypatch, tmp_path)

    assert _search(monkeypatch, first, second) == drivers


def test_the_search_takes_the_headless_shell_at_the_headless_shells_own_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The two entries of browsers.json name their builds apart: here no Chromium of either build is planted, and the
    headless shell's build is older than Chromium's."""
    root = tmp_path / "root"
    shell = _plant(root, f"chromium_headless_shell-{SHELL_BUILD}", HEADLESS_SHELL)
    _plant(root, f"chromium_headless_shell-{DRIVER_BUILD}", HEADLESS_SHELL)
    _driver(monkeypatch, tmp_path, chromium=DRIVER_BUILD, shell=SHELL_BUILD)

    assert _search(monkeypatch, root) == shell


@pytest.mark.parametrize(
    "browsers_json",
    [
        None,
        "{not json",
        json.dumps({"browsers": [{"name": "firefox", "revision": DRIVER_BUILD}]}),
        json.dumps({"browsers": [{"name": "chromium"}, {"name": "chromium-headless-shell"}]}),
    ],
    ids=["missing", "unreadable", "no-chromium-entry", "no-revision"],
)
def test_a_driver_that_names_no_chromium_build_finds_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, browsers_json: str | None
) -> None:
    """Nothing is found, so the real-browser tests skip, or fail as pytest starts under AUTO_BROWSER_REQUIRE_CHROMIUM;
    the test below turns the host suite red."""
    root = tmp_path / "root"
    _plant(root, f"chromium-{DRIVER_BUILD}")
    _plant(root, f"chromium_headless_shell-{DRIVER_BUILD}", HEADLESS_SHELL)
    _driver_file(monkeypatch, tmp_path, browsers_json)

    assert _search(monkeypatch, root) is None


def test_the_override_wins_and_needs_no_home_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A test child here runs with only PATH and SYSTEMROOT, where Path.home() raises."""
    override = _plant(tmp_path / "elsewhere", "any")
    _driver(monkeypatch, tmp_path)

    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)
    monkeypatch.setenv("AUTO_BROWSER_TEST_CHROMIUM", str(override))

    assert _chromium.chromium_executable.__wrapped__() == str(override)


def test_the_installed_driver_names_the_chromium_builds_the_search_looks_for() -> None:
    """No browser needed. Were the installed Playwright to move its browsers.json, or to name a build otherwise, the
    search would find nothing and every real-browser test would skip here; this fails instead. An entry with
    revisionOverrides installs under another folder name, which the search does not follow."""
    path = _chromium.driver_browsers_json()
    entries = _chromium.driver_chromium()

    assert path.is_file(), f"the installed Playwright has no {path}"
    for name in ("chromium", "chromium-headless-shell"):
        entry = entries.get(name, {})
        assert isinstance(entry.get("revision"), str) and entry["revision"], f"{path} names no {name!r} revision"
        assert "revisionOverrides" not in entry, f"{path}'s {name!r} entry has revisionOverrides: {entry}"


needs_the_workflow = pytest.mark.skipif(
    not CI_WORKFLOW.is_file(), reason="repo checkout not available (controller image)"
)

# The two steps' run text, compared whole: quoting, line breaks and a # inside a word each change what sh runs.
RUN_THE_SHARD = (
    "cd controller\n"
    'python -m pytest tests/ --real-browser-shard "$(( ${{ strategy.job-index }} + 1 ))/${{ strategy.job-total }}" -v\n'
)
JUDGE_THE_LEGS = (
    'gh api "repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/attempts/$GITHUB_RUN_ATTEMPT/jobs?per_page=100" \\\n'
    '  | python3 scripts/check_real_browser_legs.py "$LEGS_RESULT"\n'
)


def _jobs() -> dict[str, dict]:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _step(job: dict, runs: str) -> dict:
    """The one step of `job` whose run string holds `runs`."""
    steps = [step for step in job["steps"] if runs in step.get("run", "")]
    assert len(steps) == 1, f"{len(steps)} steps run {runs!r}"
    return steps[0]


def _whole(job: dict) -> dict:
    """The job with each action named without its version: Dependabot bumps versions, and nothing else may change."""
    steps = [{**step, "uses": step["uses"].split("@")[0]} if "uses" in step else step for step in job["steps"]]
    return {**job, "steps": steps}


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
def test_each_real_browser_leg_installs_chromium_and_runs_the_shard_of_its_position() -> None:
    """The job is held whole, an action's version and the number of shards aside, as anything else changes how the
    shards run; they are 1 to N, so that each leg's name, by which the gate reads it, gives its shard."""
    jobs = _jobs()
    job = jobs["real-browser-tests"]
    shards = job["strategy"]["matrix"]["shard"]

    assert shards == list(range(1, len(shards) + 1)) != []
    assert _whole(job) == {
        "name": "real-browser-tests (${{ matrix.shard }}/${{ strategy.job-total }})",
        "timeout-minutes": 30,
        "needs": "lint",
        "runs-on": "ubuntu-latest",
        "strategy": {"fail-fast": False, "matrix": {"shard": shards}},
        "steps": [
            {"uses": "actions/checkout"},
            {"uses": "actions/setup-python", "with": {"python-version": "3.11"}},
            {"name": "Install host test dependencies", "run": _step(jobs["host-tests"], "uv pip install")["run"]},
            {"name": "Install Chromium", "run": "python -m playwright install --with-deps chromium"},
            {
                "name": "Run this leg's shard of the real-browser tests",
                "env": {"AUTO_BROWSER_REQUIRE_CHROMIUM": "1"},
                "run": RUN_THE_SHARD,
            },
        ],
    }


@needs_the_workflow
def test_one_check_passes_only_when_the_script_passes_every_leg() -> None:
    """The job is held whole, an action's version aside; bash by name adds pipefail, so a failed API call fails the
    pipeline. scripts/check_real_browser_legs.py's own tests hold what it passes."""
    assert _whole(_jobs()["real-browser-tests-done"]) == {
        "if": "always()",
        "needs": "real-browser-tests",
        "runs-on": "ubuntu-latest",
        "permissions": {"actions": "read", "contents": "read"},
        "steps": [
            {"uses": "actions/checkout", "with": {"persist-credentials": False}},
            {
                "name": "Every leg of the real-browser tests passed",
                "shell": "bash",
                "env": {"GH_TOKEN": "${{ github.token }}", "LEGS_RESULT": "${{ needs.real-browser-tests.result }}"},
                "run": JUDGE_THE_LEGS,
            },
        ],
    }


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
