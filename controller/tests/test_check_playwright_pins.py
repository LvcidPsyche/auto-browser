"""check_playwright_pins must hold the two rules the controller (pip) and browser-node (npm) Playwright meet together.

browser-node's Playwright server refuses a client of another major.minor, and both sides must run the same browser,
which equal version strings do not give. From the pins: every place the Playwright version is pinned inside each
ecosystem agrees, package.json names no range, and the two ecosystems share a major.minor. The check once read only
controller/requirements.txt and browser-node/package.json, and stripped a range operator there, so `^1.64.1` passed
while npm could install any 1.x, and the lock npm ci installs from, and the constraints file the image installs under,
could pin another version unnoticed. From what is installed (--installed): the Chromium builds the pip driver and
npm's playwright-core name are the same.
"""

from __future__ import annotations

import inspect
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import playwright
import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
CI_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

try:
    import check_playwright_pins  # noqa: E402
except ModuleNotFoundError as exc:  # scripts/ is not shipped in the controller image
    raise unittest.SkipTest(f"check_playwright_pins unavailable: {exc}") from exc

VERSION = "1.64.1"


def _check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    requirements: str = VERSION,
    constraints: str = VERSION,
    package: str = VERSION,
    lock: str = VERSION,
    lock_core: str = VERSION,
) -> tuple[int, str]:
    paths = {
        "REQUIREMENTS": tmp_path / "requirements.txt",
        "CONSTRAINTS": tmp_path / "constraints.txt",
        "PACKAGE_JSON": tmp_path / "package.json",
        "PACKAGE_LOCK": tmp_path / "package-lock.json",
    }
    paths["REQUIREMENTS"].write_text(f"fastapi==0.1\nplaywright=={requirements}\n", encoding="utf-8")
    paths["CONSTRAINTS"].write_text(
        f"greenlet==3.0\n    # via playwright\nplaywright=={constraints}\n", encoding="utf-8"
    )
    paths["PACKAGE_JSON"].write_text(json.dumps({"dependencies": {"playwright": package}}), encoding="utf-8")
    packages = {
        "": {"dependencies": {"playwright": package}},
        "node_modules/playwright": {"version": lock},
        "node_modules/playwright-core": {"version": lock_core},
    }
    paths["PACKAGE_LOCK"].write_text(json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8")
    for name, path in paths.items():
        monkeypatch.setattr(check_playwright_pins, name, path)
    stderr = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
        code = check_playwright_pins.main()
    return code, stderr.getvalue()


def test_every_pin_equal_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _check(monkeypatch, tmp_path) == (0, "")


@pytest.mark.parametrize("spec", ["^1.64.1", "~1.64.1", ">=1.64.1", "=1.64.1", "1.64.x", "*"])
def test_a_range_in_package_json_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: str) -> None:
    code, report = _check(monkeypatch, tmp_path, package=spec)
    assert code == 1
    assert "package.json" in report and spec in report and "exact" in report


@pytest.mark.parametrize(
    ("pin", "location"),
    [
        ("lock", "package-lock.json"),
        ("lock_core", "package-lock.json"),
        ("constraints", "constraints.txt"),
        ("requirements", "requirements.txt"),
    ],
)
def test_one_pin_behind_fails_naming_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pin: str, location: str
) -> None:
    code, report = _check(monkeypatch, tmp_path, **{pin: "1.64.0"})
    assert code == 1
    behind = [line for line in report.splitlines() if line.rstrip().endswith("1.64.0")]
    assert len(behind) == 1 and location in behind[0], report


def _pins(pip: str, npm: str) -> dict[str, str]:
    return {"requirements": pip, "constraints": pip, "package": npm, "lock": npm, "lock_core": npm}


def test_another_patch_across_the_ecosystems_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The server takes a client of its own major.minor; pip 1.65.2 and npm 1.65.1 carry the same Chromium."""
    assert _check(monkeypatch, tmp_path, **_pins("1.65.2", "1.65.1")) == (0, "")


@pytest.mark.parametrize(("pip", "line"), [("1.66.0", "1.66"), ("2.65.1", "2.65")])
def test_another_major_or_minor_across_the_ecosystems_fails_naming_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pip: str, line: str
) -> None:
    code, report = _check(monkeypatch, tmp_path, **_pins(pip, "1.65.1"))

    assert code == 1
    named = [text for text in report.splitlines() if "major.minor" in text and line in text and "1.65" in text]
    assert len(named) == 1, report


CHROMIUM = {"name": "chromium", "revision": "1248", "browserVersion": "156.0.8078.4"}
SHELL = {"name": "chromium-headless-shell", "revision": "1248", "browserVersion": "156.0.8078.4"}
FIREFOX = {"name": "firefox", "revision": "1510", "browserVersion": "150.0"}


def _pip_browsers(tmp_path: Path) -> Path:
    return tmp_path / "site-packages" / "playwright" / "driver" / "package" / "browsers.json"


def _node_modules(tmp_path: Path) -> Path:
    return tmp_path / "browser-node" / "node_modules"


def _plant_browsers(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"comment": "planted", "browsers": entries}), encoding="utf-8")


def _main(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = check_playwright_pins.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _installed(
    tmp_path: Path,
    *,
    pip: list[dict] | None = None,
    npm: list[dict] | None = None,
    plant: tuple[str, ...] = ("pip", "npm"),
) -> tuple[int, str, str]:
    """The --installed check on a planted pip driver and a planted browser-node/node_modules: each side's browsers.json
    lists its entries (the same Chromium on both by default), and a side left out of `plant` has no file."""
    pip_browsers, node_modules = _pip_browsers(tmp_path), _node_modules(tmp_path)
    files = {"pip": (pip_browsers, pip), "npm": (node_modules / "playwright-core" / "browsers.json", npm)}
    for side in plant:
        path, entries = files[side]
        _plant_browsers(path, [CHROMIUM, SHELL, FIREFOX] if entries is None else entries)
    return _main(["--installed", "--pip-browsers", str(pip_browsers), "--node-modules", str(node_modules)])


def test_the_same_chromium_on_both_sides_passes(tmp_path: Path) -> None:
    code, stdout, stderr = _installed(tmp_path)

    assert (code, stderr) == (0, "")
    assert "1248" in stdout and "156.0.8078.4" in stdout


@pytest.mark.parametrize("entry", [CHROMIUM, SHELL], ids=["chromium", "headless-shell"])
@pytest.mark.parametrize(
    "change", [{"revision": "1251", "browserVersion": "159.0.8200.1"}, {"browserVersion": "156.0.8078.5"}]
)
def test_another_chromium_on_the_npm_side_fails_naming_both(tmp_path: Path, entry: dict, change: dict) -> None:
    other = {**entry, **change}
    npm = [other if item["name"] == entry["name"] else item for item in (CHROMIUM, SHELL)]

    code, _, report = _installed(tmp_path, npm=npm)

    assert code == 1
    named = [line for line in report.splitlines() if f"{entry['name']}:" in line]
    assert len(named) == 1, report
    for value in (entry["revision"], entry["browserVersion"], other["revision"], other["browserVersion"]):
        assert value in named[0], report


@pytest.mark.parametrize(
    ("side", "install"), [("pip", "pip install playwright -c controller/constraints.txt"), ("npm", "npm ci")]
)
def test_a_missing_browsers_json_fails_saying_what_to_install(tmp_path: Path, side: str, install: str) -> None:
    code, stdout, report = _installed(tmp_path, plant=tuple({"pip", "npm"} - {side}))

    assert code == 1 and "OK" not in stdout
    assert "browsers.json" in report and install in report, report


@pytest.mark.parametrize("side", ["pip", "npm"])
@pytest.mark.parametrize("entry", [CHROMIUM, SHELL], ids=["chromium", "headless-shell"])
def test_a_browsers_json_without_the_entry_fails_naming_it(tmp_path: Path, side: str, entry: dict) -> None:
    entries = [item for item in (CHROMIUM, SHELL, FIREFOX) if item["name"] != entry["name"]]

    code, _, report = _installed(tmp_path, **{side: entries})

    assert code == 1
    assert f"no {entry['name']!r} entry" in report, report


@pytest.mark.parametrize("sides", [("pip", "npm"), ("npm",)], ids=["both-sides", "npm-side"])
@pytest.mark.parametrize("value", ["missing", None, 1248], ids=["missing", "null", "a-number"])
@pytest.mark.parametrize("field", ["revision", "browserVersion"])
def test_an_entry_without_a_readable_build_fails_naming_the_file_and_the_entry(
    tmp_path: Path, field: str, value: object, sides: tuple[str, ...]
) -> None:
    """Missing on both sides, the two once compared equal (as "None") and passed; a number equal to the other side's
    string did too."""
    if value == "missing":
        unreadable = {key: item for key, item in CHROMIUM.items() if key != field}
    else:
        unreadable = {**CHROMIUM, field: value}
    named = _pip_browsers(tmp_path) if "pip" in sides else _node_modules(tmp_path) / "playwright-core" / "browsers.json"

    code, stdout, report = _installed(tmp_path, **{side: [unreadable, SHELL] for side in sides})

    assert code == 1 and "OK" not in stdout
    lines = [line for line in report.splitlines() if str(named) in line and "'chromium'" in line and field in line]
    assert len(lines) == 1, report


REVISION_OVERRIDES = {"mac12": "1240"}


@pytest.mark.parametrize("side", ["pip", "npm"])
def test_revision_overrides_on_one_side_only_fail_naming_them(tmp_path: Path, side: str) -> None:
    """An entry's revisionOverrides installs another build on the platforms it names."""
    overridden = {**CHROMIUM, "revisionOverrides": REVISION_OVERRIDES}

    code, _, report = _installed(tmp_path, **{side: [overridden, SHELL]})

    assert code == 1
    named = [line for line in report.splitlines() if line.lstrip().startswith("chromium:")]
    assert len(named) == 1 and "revisionOverrides" in named[0] and "1240" in named[0], report


def test_the_same_revision_overrides_on_both_sides_pass(tmp_path: Path) -> None:
    overridden = [{**CHROMIUM, "revisionOverrides": REVISION_OVERRIDES}, SHELL]

    code, stdout, stderr = _installed(tmp_path, pip=overridden, npm=overridden)

    assert (code, stderr) == (0, "")
    assert "revisionOverrides" in stdout


def test_the_pip_side_is_the_installed_playwrights_driver_by_default() -> None:
    """Where Playwright finds its own driver (`playwright._impl._driver.compute_driver_executable`)."""
    driver = Path(inspect.getfile(playwright)).parent / "driver" / "package" / "browsers.json"

    assert check_playwright_pins.pip_driver_browsers() == driver


@pytest.mark.parametrize("driver", ["another-chromium", "not-installed"])
def test_without_pip_browsers_the_installed_check_compares_the_installed_driver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, driver: str
) -> None:
    """As CI runs it, with no --pip-browsers: the pip side is the installed driver's file, never npm's own, and no pip
    Playwright fails."""
    pip_browsers = _pip_browsers(tmp_path)
    _plant_browsers(pip_browsers, [{**CHROMIUM, "revision": "1251"}, SHELL])
    _plant_browsers(_node_modules(tmp_path) / "playwright-core" / "browsers.json", [CHROMIUM, SHELL])
    installed = pip_browsers if driver == "another-chromium" else None
    monkeypatch.setattr(check_playwright_pins, "pip_driver_browsers", lambda: installed)

    code, stdout, report = _main(["--installed", "--node-modules", str(_node_modules(tmp_path))])

    assert code == 1 and "OK" not in stdout
    assert ("revision 1251" if installed else "pip install playwright") in report, report


needs_the_workflow = pytest.mark.skipif(
    not CI_WORKFLOW.is_file(), reason="repo checkout not available (controller image)"
)


def _step(job: dict, runs: str) -> dict:
    """The one step of `job` whose run string holds `runs`."""
    steps = [step for step in job["steps"] if runs in step.get("run", "")]
    assert len(steps) == 1, f"{len(steps)} steps run {runs!r}"
    return steps[0]


# The installed check's step runs these lines and nothing else: `|| true`, an `echo` or a dropped install would each
# keep the job green while the check never fails it.
INSTALLED_CHECK_RUN = [
    "python -m pip install playwright -c controller/constraints.txt",
    "python scripts/check_playwright_pins.py --installed",
]
# Keys by which a job runs a failing step and passes (continue-on-error), skips it (if), or gives it another shell
# (defaults).
JOB_KEYS_THAT_UNDO_A_STEP = {"if", "continue-on-error", "defaults"}


@needs_the_workflow
def test_ci_checks_the_installed_chromium_after_browser_nodes_npm_ci() -> None:
    """The lint job reads the pins only; the job that installs browser-node's packages compares the browsers. The step
    is its name and its run lines only (no if, continue-on-error or shell of its own), and its job carries nothing that
    lets it fail unseen."""
    jobs = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    installing = [
        job
        for job in jobs.values()
        if any(
            "npm ci" in step.get("run", "") and step.get("working-directory") == "browser-node" for step in job["steps"]
        )
    ]
    assert len(installing) == 1, installing
    job = installing[0]
    steps = job["steps"]

    npm_ci = steps.index(_step(job, "npm ci"))
    check = _step(job, "scripts/check_playwright_pins.py --installed")

    assert steps.index(check) > npm_ci
    assert set(check) == {"name", "run"}, check
    assert check["run"].splitlines() == INSTALLED_CHECK_RUN, check["run"]
    assert not JOB_KEYS_THAT_UNDO_A_STEP & set(job), {key: job[key] for key in JOB_KEYS_THAT_UNDO_A_STEP & set(job)}
