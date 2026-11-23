"""One shard of the real-browser tests, and a failure for each one reported skipped where they must run.

CI runs the real-browser tests (the ones `requires_chromium` marks) in a job of their own, split into shards that run
in parallel; the host-tests legs run the whole suite without a browser, so the real-browser tests skip there.
`--real-browser-shard K/N` keeps only the real-browser tests of shard K. A test's shard comes from its node id without
its parameters, so every case of one test function lands in the same shard and the N shards hold each real-browser
test exactly once.

A shard that skipped its tests would pass all the same. So where they must run (AUTO_BROWSER_REQUIRE_CHROMIUM is set,
or a shard is given), a real-browser test reported skipped fails, an expected failure included (no report tells whether
the test it stands for ran), and so does anything skipped at collection.
"""

from __future__ import annotations

import argparse
import os
import re
import zlib
from collections.abc import Generator

import pytest

from tests._chromium import REQUIRES_CHROMIUM_REASON, chromium_required

# Read once, as tests/_chromium.py reads it for the mark: a test that clears os.environ must not turn the rule off.
_BROWSER_REQUIRED = chromium_required(os.environ)
_MUST_RUN = "AUTO_BROWSER_REQUIRE_CHROMIUM is set, or --real-browser-shard is given"


def parse_shard(value: str) -> tuple[int, int]:
    """`K/N` as (K, N), for the option's type: anything but two whole numbers with 1 <= K <= N is a usage error."""
    match = re.fullmatch(r"([0-9]+)/([0-9]+)", value)
    if match is None or not 1 <= int(match.group(1)) <= int(match.group(2)):
        raise argparse.ArgumentTypeError(f"expected K/N, two whole numbers with 1 <= K <= N, got {value!r}")
    return int(match.group(1)), int(match.group(2))


def shard_of(nodeid: str, total: int) -> int:
    """The shard, 1 to `total`, of the test with this node id: everything from the first "[" (its parameters) is left
    out, so a parameter id can neither split a test function's cases nor put one case in two shards."""
    return zlib.crc32(nodeid.split("[", 1)[0].encode("utf-8")) % total + 1


def is_real_browser(item: pytest.Item) -> bool:
    """Whether the test carries `requires_chromium`, told by its reason: the mark has no name of its own."""
    return any(mark.kwargs.get("reason") == REQUIRES_CHROMIUM_REASON for mark in item.iter_markers(name="skipif"))


def _must_run(config: pytest.Config) -> bool:
    return _BROWSER_REQUIRED or config.getoption("real_browser_shard") is not None


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--real-browser-shard",
        type=parse_shard,
        metavar="K/N",
        help="run only the real-browser tests (the ones requires_chromium marks) of shard K of N, and fail any "
        "reported skipped",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = config.getoption("real_browser_shard")
    if shard is None:
        return
    number, total = shard
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        keep = is_real_browser(item) and shard_of(item.nodeid, total) == number
        (selected if keep else deselected).append(item)
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    """What is skipped at collection (a module, or any collector) holds no tests to tell real-browser ones by, so where
    they must run, any skip at collection fails the run (pytest reports it as a collection error)."""
    report = yield
    if report.skipped and _must_run(collector.config):
        path, lineno, reason = report.longrepr
        report.outcome = "failed"
        report.longrepr = (
            f"{report.nodeid} was skipped at collection ({path}:{lineno}: {reason}), where real-browser tests must "
            f"run ({_MUST_RUN}): nothing tells whether its tests are real-browser ones, so skip its tests one by one "
            "instead"
        )
    return report


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Turn a real-browser test reported skipped, an expected failure included, into a failure where they must run,
    as pytest turns a strict xfail that passed into one."""
    report = yield
    if report.skipped and is_real_browser(item) and _must_run(item.config):
        if hasattr(report, "wasxfail"):
            # pytest's own text (the traceback of what was raised) says where and why, as a skip's location does.
            said, detail = "as an expected failure", f"{report.wasxfail}\n{report.longrepr}"
            # A failed report that keeps its wasxfail is printed as a failure or an error and counted in the summary, but
            # pytest leaves it out of the run's exit status.
            del report.wasxfail
        else:
            path, lineno, reason = report.longrepr  # a skip: where it was skipped, and why
            said, detail = "skipped", f"{path}:{lineno}: {reason}"
        report.longrepr = (
            f"a real-browser test was reported {said} in its {report.when} phase, where real-browser tests must run "
            f"({_MUST_RUN}): {detail}"
        )
        report.outcome = "failed"
    return report
