"""scripts/check_real_browser_legs.py, the judge behind ci.yml's one required check for the real-browser tests.

It reads a run attempt's jobs listing and the legs' aggregate result, and passes only a whole set of legs that all
succeeded. The judging function runs on listings built here and on a recorded one; the last tests run the command as a
child, for its exit codes and what it prints where.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_real_browser_legs.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.parent.is_dir(), reason="scripts/ is not shipped in the controller image")

FOUR = [(f"real-browser-tests ({number}/4)", "success") for number in range(1, 5)]
FOUR_PASSED = [f"real-browser-tests ({number}/4): success" for number in range(1, 5)]

# Run 43775524181's attempt 1 as GitHub listed it, reduced to the fields the script reads, with host-tests' two legs
# renamed as real-browser legs.
RECORDED = {
    "total_count": 10,
    "jobs": [
        {"name": "lint", "conclusion": "success"},
        {"name": "dependency-audit", "conclusion": "success"},
        {"name": "client-tests", "conclusion": "success"},
        {"name": "real-browser-tests (1/2)", "conclusion": "success"},
        {"name": "controller-tests", "conclusion": "failure"},
        {"name": "langchain-tests (3.14)", "conclusion": "success"},
        {"name": "real-browser-tests (2/2)", "conclusion": "success"},
        {"name": "package-build", "conclusion": "success"},
        {"name": "langchain-tests (3.11)", "conclusion": "success"},
        {"name": "compose-smoke", "conclusion": "skipped"},
    ],
}


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("check_real_browser_legs_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _listing(*legs: tuple[str, str | None], total: int | None = None) -> dict:
    jobs = [{"name": name, "conclusion": conclusion} for name, conclusion in (("lint", "success"), *legs)]
    return {"total_count": len(jobs) if total is None else total, "jobs": jobs}


def _failure(check, listing: object, aggregate: str = "success") -> str:
    """The one line a failing judgement prints."""
    passed, lines = check.judge(listing, aggregate)
    assert not passed and len(lines) == 1, lines
    return lines[0]


def test_four_legs_that_all_passed_pass_and_each_is_printed(check) -> None:
    assert check.judge(_listing(*FOUR), "success") == (True, FOUR_PASSED)


def test_a_recorded_listing_with_two_legs_passes(check) -> None:
    legs = ["real-browser-tests (1/2): success", "real-browser-tests (2/2): success"]

    assert check.judge(RECORDED, "success") == (True, legs)


def test_legs_in_any_order_pass_and_print_in_the_listings_order(check) -> None:
    assert check.judge(_listing(*FOUR[::-1]), "success") == (True, FOUR_PASSED[::-1])


@pytest.mark.parametrize("conclusion", ["cancelled", "failure", "timed_out", "skipped", None])
def test_a_leg_that_did_not_succeed_fails_even_when_the_aggregate_says_success(check, conclusion: str | None) -> None:
    line = _failure(check, _listing(*FOUR[:2], ("real-browser-tests (3/4)", conclusion), FOUR[3]))

    assert "not every leg succeeded" in line
    assert f"real-browser-tests (3/4): {'null' if conclusion is None else conclusion}" in line
    assert "real-browser-tests (4/4): success" in line


@pytest.mark.parametrize("aggregate", ["failure", "skipped", "cancelled"])
def test_four_good_legs_fail_when_the_aggregate_is_not_success(check, aggregate: str) -> None:
    assert f"the aggregate result is {aggregate}" in _failure(check, _listing(*FOUR), aggregate)


@pytest.mark.parametrize(
    "legs, said",
    [
        ([], "no leg"),
        (FOUR[:3], "the legs are not 1 to 4 once each"),
        ([FOUR[0], FOUR[1], FOUR[1], FOUR[3]], "the legs are not 1 to 4 once each"),
        ([*FOUR[:3], ("real-browser-tests (4/5)", "success")], "the legs do not share one N"),
        ([("real-browser-tests (other)", "success")], "not named as a leg"),
        ([*FOUR, ("real-browser-tests (other)", "success")], "not named as a leg"),
        ([*FOUR[:3], ("real-browser-tests (04/4)", "success")], "not named as a leg"),
        ([("real-browser-tests (1/100000000000000000000)", "success")], "not named as a leg"),
        ([(f"real-browser-tests ({'1' * 5000}/4)", "success"), *FOUR[1:]], "not named as a leg"),
    ],
    ids=[
        "no leg",
        "three of four",
        "a repeated K",
        "two Ns",
        "a stray name alone",
        "a stray name beside four",
        "04/4",
        "an N past what a list holds",
        "a K of 5,000 digits",
    ],
)
def test_legs_that_are_not_a_whole_set_fail(check, legs: list[tuple[str, str]], said: str) -> None:
    assert said in _failure(check, _listing(*legs))


def test_a_name_that_is_not_a_legs_is_quoted_so_the_failure_stays_one_line(check) -> None:
    line = _failure(check, _listing(*FOUR, ("real-browser-tests (1/4)\nreal-browser-tests (5/4)", "success")))

    assert "\n" not in line and "'real-browser-tests (1/4)\\nreal-browser-tests (5/4)'" in line, line


@pytest.mark.parametrize("total", [150, 2])
def test_a_listing_that_does_not_hold_its_total_count_of_jobs_fails(check, total: int) -> None:
    line = _failure(check, _listing(*FOUR, total=total))

    assert f"the listing holds 5 jobs but its total_count says {total}" in line


# Each wrong shape, and what the failure says of it.
WRONG_SHAPES = {
    "a list": ([1, 2], "not an object"),
    "an API error": ({"message": "Not Found"}, "no total_count"),
    "no jobs": ({"total_count": 1}, "no jobs"),
    "a job with no conclusion": ({"total_count": 1, "jobs": [{"name": "lint"}]}, "a job with no name or conclusion"),
    "a count that is not a number": ({"total_count": "1", "jobs": []}, "no total_count"),
    "a count of true beside one leg": (
        {"total_count": True, "jobs": [{"name": "real-browser-tests (1/1)", "conclusion": "success"}]},
        "no total_count",
    ),
}


@pytest.mark.parametrize("listing, why", list(WRONG_SHAPES.values()), ids=list(WRONG_SHAPES))
def test_a_listing_of_the_wrong_shape_fails_with_a_message(check, listing: object, why: str) -> None:
    assert f"not a jobs listing ({why}" in _failure(check, listing)


def _command(stdin: str, *args: str) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(_SCRIPT), *args]
    return subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=60)


def test_the_command_reads_the_listing_on_stdin_and_the_aggregate_as_its_one_argument() -> None:
    passed = _command(json.dumps(_listing(*FOUR)), "success")
    failed = _command(json.dumps(_listing(*FOUR)), "failure")
    usage = _command(json.dumps(_listing(*FOUR)))

    assert (passed.returncode, passed.stdout.splitlines(), passed.stderr) == (0, FOUR_PASSED, "")
    assert (failed.returncode, failed.stdout) == (1, "") and len(failed.stderr.splitlines()) == 1, failed.stderr
    assert "the aggregate result is failure" in failed.stderr
    assert usage.returncode == 2 and "usage:" in usage.stderr


@pytest.mark.parametrize(
    "stdin", ["", "not json", '{"message": "Not Found"}'], ids=["nothing", "not JSON", "an API error"]
)
def test_input_that_is_not_a_jobs_listing_fails_with_a_message_not_a_traceback(stdin: str) -> None:
    result = _command(stdin, "success")

    assert result.returncode == 1, result.stderr
    assert "not a jobs listing" in result.stderr and "Traceback" not in result.stderr, result.stderr
