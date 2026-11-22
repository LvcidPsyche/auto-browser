"""`--real-browser-shard K/N` runs one shard of the real-browser tests, and a run that must run them fails where one
does not run.

The shard function is tested directly; the hooks run in a pytest child on a small tree of its own, loaded with `-p`.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pytest

from tests import _real_browser_shards
from tests._real_browser_shards import parse_shard, shard_of
from tests.test_chromium_requirement import MISSING, _child, _pytest_on

PLUGIN = ("-p", "tests._real_browser_shards")

# The three ways a real-browser test takes the mark: its module's pytestmark (over a class and a parametrized test
# too), another module's pytestmark imported by name (as the element-id files take test_element_ids'), and the
# decorator; beside them, tests that do not take it.
TREE = {
    "test_marked.py": """
import pytest

from tests._chromium import requires_chromium

pytestmark = requires_chromium


def test_one():
    pass


@pytest.mark.parametrize("case", ["a", "b", "c[d]", "e::f", "g"])
def test_cases(case):
    pass


class TestGroup:
    def test_in_a_class(self):
        pass
""",
    "test_borrowed.py": """
from test_marked import pytestmark  # noqa: F401


def test_borrowed():
    pass
""",
    "test_mixed.py": """
import pytest

from tests._chromium import requires_chromium


@requires_chromium
def test_decorated():
    pass


@pytest.mark.skipif(False, reason="another condition")
def test_unmarked():
    pass


@pytest.mark.parametrize("case", [1, 2, 3])
def test_unmarked_cases(case):
    pass
""",
}
MARKED = {
    "test_marked.py::test_one",
    *(f"test_marked.py::test_cases[{case}]" for case in ["a", "b", "c[d]", "e::f", "g"]),
    "test_marked.py::TestGroup::test_in_a_class",
    "test_borrowed.py::test_borrowed",
    "test_mixed.py::test_decorated",
}
UNMARKED = {"test_mixed.py::test_unmarked", *(f"test_mixed.py::test_unmarked_cases[{case}]" for case in [1, 2, 3])}


def _tree(tmp_path: Path) -> Path:
    for name, text in TREE.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def _collected(tree: Path, *options: str) -> tuple[set[str], int]:
    """The node ids a collection of the tree keeps, and the number it reports deselected."""
    result = _child(_pytest_on(tree, *PLUGIN, "--collect-only", "-q", *options), chromium=MISSING, required=None)
    # 5: every test of the tree deselected, a shard that holds none of its tests.
    assert result.returncode in (0, 5), result.stdout + result.stderr
    deselected = re.search(r"\((\d+) deselected\)", result.stdout)
    return {line for line in result.stdout.splitlines() if "::" in line}, int(deselected[1]) if deselected else 0


def test_every_case_of_a_test_lands_in_the_shard_of_its_function() -> None:
    for total in (1, 2, 3, 4, 7):
        function = shard_of("tests/test_x.py::test_y", total)
        cases = ("a", "b-1", "[nested]", "c::d", "")
        assert {shard_of(f"tests/test_x.py::test_y[{case}]", total) for case in cases} == {function}


def test_the_shards_are_one_to_n_and_each_is_used() -> None:
    nodeids = [f"tests/test_{module}.py::test_{function}" for module in range(20) for function in range(20)]
    for total in (1, 2, 4, 9):
        assert {shard_of(nodeid, total) for nodeid in nodeids} == set(range(1, total + 1))


def test_the_shard_of_a_test_is_pinned() -> None:
    """crc32 of the node id in UTF-8, modulo N, plus one."""
    assert shard_of("tests/test_page_text.py::test_real_chromium_excerpt_keeps_a_table_readable", 4) == 3


@pytest.mark.parametrize("value, expected", [("1/1", (1, 1)), ("3/4", (3, 4)), ("4/4", (4, 4)), ("12/40", (12, 40))])
def test_a_shard_is_k_of_n(value: str, expected: tuple[int, int]) -> None:
    assert parse_shard(value) == expected


MALFORMED = ["", "4", "0/4", "5/4", "1/0", "a/4", "1/b", "1/4/2", " 1/4", "1/4 ", "+1/4", "1.0/4", "١/٤"]


@pytest.mark.parametrize("value", MALFORMED)
def test_a_malformed_shard_is_refused(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match=r"K/N"):
        parse_shard(value)


def test_without_the_option_every_test_is_collected(tmp_path: Path) -> None:
    assert _collected(_tree(tmp_path)) == (MARKED | UNMARKED, 0)


@pytest.mark.parametrize("total", [2, 4])
def test_the_shards_hold_each_real_browser_test_exactly_once(tmp_path: Path, total: int) -> None:
    """Together the shards hold the marked tests, the borrowed one included, and nothing unmarked; no two hold the
    same test, each holds only tests of its own number, and each reports the rest of the tree deselected."""
    tree = _tree(tmp_path)
    shards = [_collected(tree, "--real-browser-shard", f"{number}/{total}") for number in range(1, total + 1)]

    assert set().union(*(kept for kept, _ in shards)) == MARKED
    assert sum(len(kept) for kept, _ in shards) == len(MARKED)
    for number, (kept, deselected) in enumerate(shards, start=1):
        assert {shard_of(nodeid, total) for nodeid in kept} <= {number}
        assert deselected == len(MARKED | UNMARKED) - len(kept)


def test_a_malformed_shard_is_a_usage_error(tmp_path: Path) -> None:
    args = _pytest_on(_tree(tmp_path), *PLUGIN, "--real-browser-shard", "5/4")
    result = _child(args, chromium=MISSING, required=None)

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "--real-browser-shard" in result.stderr and "'5/4'" in result.stderr


def test_the_suite_runs_every_hook_of_the_module(pytestconfig: pytest.Config) -> None:
    """The child runs here load the module with -p, so they pass whatever tests/conftest.py imports: this checks the
    suite itself holds each of the module's hooks, one added later included."""
    hooks = {name: value for name, value in vars(_real_browser_shards).items() if name.startswith("pytest_")}

    assert hooks
    for name, function in hooks.items():
        assert function in [impl.function for impl in getattr(pytestconfig.hook, name).get_hookimpls()], name


SKIPS = """
import os

import pytest

from tests._chromium import requires_chromium


@requires_chromium
@pytest.mark.skipif(True, reason="a second condition")
def test_marked_with_a_second_skipif():
    pass


@requires_chromium
def test_marked_that_skips_itself():
    pytest.skip("skipped inside")


@requires_chromium
def test_marked_that_runs():
    pass


@pytest.mark.skip(reason="not a real-browser test")
def test_unmarked_that_skips():
    pass


@requires_chromium
def test_marked_that_clears_the_environment_and_skips():
    os.environ.clear()
    pytest.skip("skipped with the environment cleared")
"""

XFAILS = """
import pytest

from tests._chromium import requires_chromium


@requires_chromium
@pytest.mark.xfail(run=False, reason="never run")
def test_marked_xfail_not_run():
    raise AssertionError


@pytest.fixture
def xfails_in_setup():
    pytest.xfail("in a fixture")


@requires_chromium
def test_marked_xfail_in_a_fixture(xfails_in_setup):
    raise AssertionError


@requires_chromium
@pytest.mark.xfail(reason="expected", strict=True)
def test_marked_xfail_that_runs():
    raise AssertionError
"""

SKIPPED_MODULE = """
import pytest

from tests._chromium import requires_chromium

pytestmark = requires_chromium
pytest.importorskip("a_module_this_tree_does_not_have")


def test_never_collected():
    pass
"""

RUNS = "from tests._chromium import requires_chromium\n\n\n@requires_chromium\ndef test_runs():\n    pass\n"

# The start of the failure each test that did not run is reported with, up to where it says why.
DID_NOT_RUN = r"a real-browser test did not run where they must [^\n]*?: "


def _run(tmp_path: Path, name: str, text: str, *options: str, required: str | None) -> tuple[int, str, dict[str, str]]:
    """The exit code, the output and each test's outcome (as `-v` prints it) of a run of `text`, with a Chromium."""
    (tmp_path / name).write_text(text, encoding="utf-8")
    result = _child(_pytest_on(tmp_path, *PLUGIN, "-v", *options), chromium=sys.executable, required=required)
    # `-v` prints each node id relative to the working directory, the controller, so the tree's path comes before it.
    outcomes = re.findall(rf"{re.escape(name)}::(\w+) (PASSED|FAILED|ERROR|SKIPPED|XFAIL)", result.stdout)
    return result.returncode, result.stdout + result.stderr, dict(outcomes)


def test_with_the_switch_a_real_browser_test_that_skips_fails_the_run(tmp_path: Path) -> None:
    """A skip in setup (the second skipif) is reported as an error, one in the test as a failure, each with where it was
    skipped and why; an unmarked test may skip. Clearing the environment does not turn the rule off."""
    returncode, output, outcomes = _run(tmp_path, "test_skips.py", SKIPS, required="1")

    assert returncode == pytest.ExitCode.TESTS_FAILED, output
    assert outcomes == {
        "test_marked_with_a_second_skipif": "ERROR",
        "test_marked_that_skips_itself": "FAILED",
        "test_marked_that_runs": "PASSED",
        "test_unmarked_that_skips": "SKIPPED",
        "test_marked_that_clears_the_environment_and_skips": "FAILED",
    }, output
    for reason in ("a second condition", "skipped inside", "skipped with the environment cleared"):
        assert re.search(rf"{DID_NOT_RUN}\S+test_skips\.py:\d+: Skipped: {reason}\n", output), reason


def test_without_the_rule_a_real_browser_test_that_skips_leaves_the_run_green(tmp_path: Path) -> None:
    returncode, output, outcomes = _run(tmp_path, "test_skips.py", SKIPS, required=None)

    assert returncode == pytest.ExitCode.OK, output
    assert outcomes == {
        "test_marked_with_a_second_skipif": "SKIPPED",
        "test_marked_that_skips_itself": "SKIPPED",
        "test_marked_that_runs": "PASSED",
        "test_unmarked_that_skips": "SKIPPED",
        "test_marked_that_clears_the_environment_and_skips": "SKIPPED",
    }, output


def test_the_shard_option_holds_the_rule_without_the_switch(tmp_path: Path) -> None:
    """A shard whose tests all skipped would pass having run nothing."""
    returncode, output, outcomes = _run(tmp_path, "test_skips.py", SKIPS, "--real-browser-shard", "1/1", required=None)

    assert returncode == pytest.ExitCode.TESTS_FAILED, output
    assert outcomes == {
        "test_marked_with_a_second_skipif": "ERROR",
        "test_marked_that_skips_itself": "FAILED",
        "test_marked_that_runs": "PASSED",
        "test_marked_that_clears_the_environment_and_skips": "FAILED",
    }, output


def test_with_the_switch_an_expected_failure_that_never_ran_fails_the_run(tmp_path: Path) -> None:
    """Only an expected failure reported from the call phase ran its body; one marked not to run, or raised by a
    fixture, fails with its reason. A run of these alone, so that its exit code shows each counts as a failure."""
    returncode, output, outcomes = _run(tmp_path, "test_xfails.py", XFAILS, required="1")

    assert returncode == pytest.ExitCode.TESTS_FAILED, output
    assert outcomes == {
        "test_marked_xfail_not_run": "ERROR",
        "test_marked_xfail_in_a_fixture": "ERROR",
        "test_marked_xfail_that_runs": "XFAIL",
    }, output
    for reason in (r"\[NOTRUN\] never run", "in a fixture"):
        assert re.search(rf"{DID_NOT_RUN}an expected failure that never ran: {reason}\n", output), reason


def _beside_a_skipped_module(tmp_path: Path, *, required: str | None) -> tuple[int, str, dict[str, str]]:
    (tmp_path / "test_skipped_module.py").write_text(SKIPPED_MODULE, encoding="utf-8")
    return _run(tmp_path, "test_runs.py", RUNS, required=required)


def test_with_the_switch_a_module_skipped_at_collection_fails_the_run(tmp_path: Path) -> None:
    """Such a module has no tests to tell real-browser ones by, so any module skipped at collection fails."""
    returncode, output, _ = _beside_a_skipped_module(tmp_path, required="1")

    assert returncode == pytest.ExitCode.INTERRUPTED, output
    assert re.search(r"ERROR collecting \S*test_skipped_module\.py", output), output
    assert "a_module_this_tree_does_not_have" in output and "skip its tests one by one instead" in output, output


def test_without_the_rule_a_module_skipped_at_collection_leaves_the_run_green(tmp_path: Path) -> None:
    returncode, output, outcomes = _beside_a_skipped_module(tmp_path, required=None)

    assert returncode == pytest.ExitCode.OK, output
    assert outcomes == {"test_runs": "PASSED"} and "1 passed, 1 skipped" in output, output
