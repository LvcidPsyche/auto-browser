"""`--real-browser-shard K/N` runs one shard of the real-browser tests, and under AUTO_BROWSER_REQUIRE_CHROMIUM a
real-browser test that skips fails the run.

CI runs the real-browser tests (the ones `requires_chromium` marks) in shards that run in parallel, and every one of
them must run exactly once in a round. The shard function is tested directly; the hooks run in a pytest child on a
small tree of its own, loaded with `-p` as tests/conftest.py loads them for the real tree.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pytest

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


def _collected(tree: Path, *options: str) -> set[str]:
    """The node ids a collection of the tree keeps."""
    result = _child(_pytest_on(tree, *PLUGIN, "--collect-only", "-q", *options), chromium=MISSING, required=None)
    # 5: every test of the tree deselected, a shard that holds none of its tests.
    assert result.returncode in (0, 5), result.stdout + result.stderr
    return {line for line in result.stdout.splitlines() if "::" in line}


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
    """crc32 of the node id in UTF-8, modulo N, plus one (in Latin-1 the last example would land in shard 3)."""
    assert shard_of("tests/test_page_text.py::test_real_chromium_excerpt_keeps_a_table_readable", 4) == 3
    float_values = "tests/test_stealth_real_chromium.py::test_a_read_of_float_values_is_left_as_it_is"
    assert [shard_of(float_values, total) for total in (1, 2, 3, 4)] == [1, 2, 3, 4]
    assert shard_of("tests/test_é.py::test_ü", 4) == 4


@pytest.mark.parametrize("value, expected", [("1/1", (1, 1)), ("3/4", (3, 4)), ("4/4", (4, 4)), ("12/40", (12, 40))])
def test_a_shard_is_k_of_n(value: str, expected: tuple[int, int]) -> None:
    assert parse_shard(value) == expected


MALFORMED = ["", "4", "0/4", "5/4", "1/0", "0/0", "-1/4", "a/4", "1/b", "1/4/2", " 1/4", "1/4 ", "+1/4", "1.0/4", "١/٤"]


@pytest.mark.parametrize("value", MALFORMED)
def test_a_malformed_shard_is_refused(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match=r"K/N"):
        parse_shard(value)


def test_without_the_option_every_test_is_collected(tmp_path: Path) -> None:
    assert _collected(_tree(tmp_path)) == MARKED | UNMARKED


@pytest.mark.parametrize("total", [1, 2, 4])
def test_the_shards_hold_each_real_browser_test_exactly_once(tmp_path: Path, total: int) -> None:
    """Together the shards hold the marked tests, the borrowed one included, and nothing unmarked; no two hold the
    same test, and each holds only tests of its own number."""
    tree = _tree(tmp_path)
    shards = [_collected(tree, "--real-browser-shard", f"{number}/{total}") for number in range(1, total + 1)]

    assert set().union(*shards) == MARKED
    assert sum(len(shard) for shard in shards) == len(MARKED)
    for number, shard in enumerate(shards, start=1):
        assert {shard_of(nodeid, total) for nodeid in shard} <= {number}


def test_a_malformed_shard_is_a_usage_error(tmp_path: Path) -> None:
    args = _pytest_on(_tree(tmp_path), *PLUGIN, "--real-browser-shard", "5/4")
    result = _child(args, chromium=MISSING, required=None)

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "--real-browser-shard" in result.stderr and "'5/4'" in result.stderr


SKIPS = """
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
@pytest.mark.xfail(reason="expected", strict=True)
def test_marked_that_xfails():
    raise AssertionError


@requires_chromium
def test_marked_that_runs():
    pass


@pytest.mark.skip(reason="not a real-browser test")
def test_unmarked_that_skips():
    pass
"""


def _run_skips(tmp_path: Path, *, required: str | None) -> tuple[int, str, dict[str, str]]:
    """The exit code, the output and each test's outcome (as `-v` prints it) of a run of SKIPS with a Chromium."""
    (tmp_path / "test_skips.py").write_text(SKIPS, encoding="utf-8")
    result = _child(_pytest_on(tmp_path, *PLUGIN, "-v"), chromium=sys.executable, required=required)
    # `-v` prints each node id relative to the working directory, the controller, so the tree's path comes before it.
    outcomes = re.findall(r"test_skips\.py::(\w+) (PASSED|FAILED|ERROR|SKIPPED|XFAIL)", result.stdout)
    return result.returncode, result.stdout + result.stderr, dict(outcomes)


def test_with_the_switch_a_real_browser_test_that_skips_fails_the_run(tmp_path: Path) -> None:
    """A skip in setup (the second skipif) is reported as an error, one in the test as a failure, each with the reason
    it skipped. An expected failure ran, and an unmarked test may skip."""
    returncode, output, outcomes = _run_skips(tmp_path, required="1")

    assert returncode == pytest.ExitCode.TESTS_FAILED, output
    assert outcomes == {
        "test_marked_with_a_second_skipif": "ERROR",
        "test_marked_that_skips_itself": "FAILED",
        "test_marked_that_xfails": "XFAIL",
        "test_marked_that_runs": "PASSED",
        "test_unmarked_that_skips": "SKIPPED",
    }, output
    for reason in ("a second condition", "skipped inside"):
        assert re.search(rf"a real-browser test skipped where a browser is required.*: Skipped: {reason}\n", output)


def test_without_the_switch_a_real_browser_test_that_skips_leaves_the_run_green(tmp_path: Path) -> None:
    returncode, output, outcomes = _run_skips(tmp_path, required=None)

    assert returncode == pytest.ExitCode.OK, output
    assert outcomes == {
        "test_marked_with_a_second_skipif": "SKIPPED",
        "test_marked_that_skips_itself": "SKIPPED",
        "test_marked_that_xfails": "XFAIL",
        "test_marked_that_runs": "PASSED",
        "test_unmarked_that_skips": "SKIPPED",
    }, output
