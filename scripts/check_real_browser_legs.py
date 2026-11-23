#!/usr/bin/env python3
"""Judge the legs of ci.yml's real-browser-tests job, for real-browser-tests-done, the one check that stands for them.

Reads a run attempt's jobs listing (GitHub's "list jobs for a workflow run attempt", as JSON) on stdin, and takes the
legs' aggregate result (needs.real-browser-tests.result) as its one argument. GitHub's aggregate alone has passed a
leg that was cancelled before it started, so each leg is judged as well.

Exit 0, printing each leg with its conclusion, only when the aggregate is success, the listing is whole (it holds as
many jobs as its total_count says), the legs (the jobs whose names start "real-browser-tests (") are named
real-browser-tests (1/N) to (N/N) once each, N their number, and every leg succeeded. A failed check exits 1, with
one line that says what failed and each leg's conclusion, or, when the listing cannot be read, why not; a call
without the one argument is a usage error, exit 2.
"""

from __future__ import annotations

import json
import sys

PREFIX = "real-browser-tests ("


def _jobs(listing: object) -> tuple[list[tuple[str, str | None]], int]:
    """Each job's (name, conclusion), and the attempt's number of jobs; ValueError when the listing is not GitHub's."""
    if not isinstance(listing, dict):
        raise ValueError("not an object")
    # type(), not isinstance(): a bool is an int to isinstance(), and true is no count.
    if type(listing.get("total_count")) is not int:
        raise ValueError("no total_count")
    if not isinstance(listing.get("jobs"), list):
        raise ValueError("no jobs")
    jobs = []
    for job in listing["jobs"]:
        if not isinstance(job, dict) or not isinstance(job.get("name"), str) or "conclusion" not in job:
            raise ValueError(f"a job with no name or conclusion: {json.dumps(job)[:120]}")
        jobs.append((job["name"], job["conclusion"]))
    return jobs, listing["total_count"]


def _not_a_whole_set(names: list[str]) -> str | None:
    """What keeps the legs' names from being real-browser-tests (1/N) to (N/N) once each, N their number, or None."""
    if not names:
        return "no leg"
    if sorted(names) != sorted(f"{PREFIX}{number}/{len(names)})" for number in range(1, len(names) + 1)):
        return "the legs are not real-browser-tests (1/N) to (N/N) once each"
    return None


def _failure(why: str) -> str:
    """The one line a failed check prints: quoted when it holds a line break or another unprintable character."""
    line = f"real-browser-tests did not pass: {why}"
    return line if line.isprintable() else repr(line)


def judge(listing: object, aggregate: str) -> tuple[bool, list[str]]:
    """Whether the legs passed, and the lines to print: one per leg if they did, otherwise one that says why."""
    try:
        jobs, total_count = _jobs(listing)
    except ValueError as error:
        return False, [_failure(f"not a jobs listing ({error})")]
    legs = [(name, conclusion) for name, conclusion in jobs if name.startswith(PREFIX)]
    problems = []
    if aggregate != "success":
        problems.append(f"the aggregate result is {aggregate}")
    if total_count != len(jobs):
        problems.append(f"the listing holds {len(jobs)} jobs but its total_count says {total_count}")
    shape = _not_a_whole_set([name for name, _ in legs])
    if shape:
        problems.append(shape)
    if any(conclusion != "success" for _, conclusion in legs):
        problems.append("not every leg succeeded")
    concluded = [f"{name}: {'null' if conclusion is None else conclusion}" for name, conclusion in legs]
    if problems:
        each = ", ".join(concluded) or "none"
        return False, [_failure(f"{'; '.join(problems)}. Legs: {each}.")]
    return True, concluded


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check_real_browser_legs.py AGGREGATE_RESULT < jobs-listing.json", file=sys.stderr)
        return 2
    try:
        listing = json.loads(sys.stdin.read())
    except ValueError as error:
        print(_failure(f"not a jobs listing (not JSON: {error})"), file=sys.stderr)
        return 1
    passed, lines = judge(listing, argv[0])
    print("\n".join(lines), file=sys.stdout if passed else sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
