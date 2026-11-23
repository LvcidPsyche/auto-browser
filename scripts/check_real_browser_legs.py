#!/usr/bin/env python3
"""Judge the legs of ci.yml's real-browser-tests job, for real-browser-tests-done, the one check that stands for them.

Reads a run attempt's jobs listing (GitHub's "list jobs for a workflow run attempt", as JSON) on stdin, and takes the
legs' aggregate result (needs.real-browser-tests.result) as its one argument. GitHub's aggregate alone has passed a
leg that was cancelled before it started, so each leg is judged as well.

Exit 0, printing each leg with its conclusion, only when the aggregate is success, the listing is whole (one page holds
every job), the legs (the jobs named "real-browser-tests (K/N)") share one N and hold each K from 1 to N once, and
every leg succeeded. A job named like a leg but not of that shape fails the check. Otherwise exit 1, with one line
that says what failed and each leg's conclusion.
"""

from __future__ import annotations

import json
import re
import sys

PREFIX = "real-browser-tests ("
LEG = re.compile(r"real-browser-tests \(([1-9][0-9]*)/([1-9][0-9]*)\)")


def _jobs(listing: object) -> list[tuple[str, str | None]]:
    """Each job's (name, conclusion); ValueError when the listing is not GitHub's jobs listing."""
    if not isinstance(listing, dict) or type(listing.get("total_count")) is not int:
        raise ValueError("no total_count")
    if not isinstance(listing.get("jobs"), list):
        raise ValueError("no jobs")
    jobs = []
    for job in listing["jobs"]:
        if not isinstance(job, dict) or not isinstance(job.get("name"), str) or "conclusion" not in job:
            raise ValueError(f"a job with no name or conclusion: {json.dumps(job)[:120]}")
        jobs.append((job["name"], job["conclusion"]))
    return jobs


def judge(listing: object, aggregate: str) -> tuple[bool, list[str]]:
    """Whether the legs passed, and the lines to print: one per leg if they did, otherwise one that says why."""
    try:
        jobs = _jobs(listing)
    except ValueError as error:
        return False, [f"real-browser-tests did not pass: not a jobs listing ({error})"]
    legs = [(name, conclusion) for name, conclusion in jobs if name.startswith(PREFIX)]
    matches = [LEG.fullmatch(name) for name, _ in legs]
    numbers = sorted(int(match[1]) for match in matches if match)
    totals = {int(match[2]) for match in matches if match}
    problems = []
    if aggregate != "success":
        problems.append(f"the aggregate result is {aggregate}")
    if listing["total_count"] != len(jobs):
        problems.append(f"the listing holds {len(jobs)} of {listing['total_count']} jobs, so it needs paging")
    stray = [name for (name, _), match in zip(legs, matches) if match is None]
    if stray:
        problems.append(f"not named as a leg, real-browser-tests (K/N): {', '.join(stray)}")
    if not legs:
        problems.append("no leg")
    elif len(totals) > 1:
        problems.append("the legs do not share one N")
    elif totals and numbers != list(range(1, max(totals) + 1)):
        problems.append(f"the legs are not 1 to {max(totals)} once each")
    if any(conclusion != "success" for _, conclusion in legs):
        problems.append("not every leg succeeded")
    concluded = [f"{name}: {'null' if conclusion is None else conclusion}" for name, conclusion in legs]
    if problems:
        each = ", ".join(concluded) or "none"
        return False, [f"real-browser-tests did not pass: {'; '.join(problems)}. Legs: {each}."]
    return True, [line for _, line in sorted(zip((int(match[1]) for match in matches), concluded))]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check_real_browser_legs.py AGGREGATE_RESULT < jobs-listing.json", file=sys.stderr)
        return 2
    try:
        listing = json.loads(sys.stdin.read())
    except ValueError as error:
        print(f"real-browser-tests did not pass: not a jobs listing (not JSON: {error})", file=sys.stderr)
        return 1
    passed, lines = judge(listing, argv[0])
    print("\n".join(lines), file=sys.stdout if passed else sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
