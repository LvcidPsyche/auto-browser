"""One shard of the real-browser tests, and no real-browser test skipped where a browser is required.

CI runs the real-browser tests (the ones `requires_chromium` marks) in a job of their own, split into shards that run
in parallel, and the host-tests legs run the rest of the suite without a browser. `--real-browser-shard K/N` keeps only
the real-browser tests of shard K. A test's shard comes from its node id without its parameters, so every case of one
test function lands in the same shard and the N shards hold each real-browser test exactly once.

A shard that skipped its tests would pass all the same, so where AUTO_BROWSER_REQUIRE_CHROMIUM is set a real-browser
test that skips, for any reason, is reported as failed, with the reason it skipped.

tests/conftest.py imports the hooks by name, which registers them for the suite; a test loads this module into a
pytest child with `-p`.
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


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--real-browser-shard",
        type=parse_shard,
        metavar="K/N",
        help="run only the real-browser tests (the ones requires_chromium marks) of shard K of N",
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
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Turn the skip of a real-browser test into a failure where a browser is required, as pytest turns a strict xfail
    that passed into one. An expected failure (reported as skipped, with `wasxfail`) ran, so it stays as it is."""
    report = yield
    if _BROWSER_REQUIRED and report.skipped and not hasattr(report, "wasxfail") and is_real_browser(item):
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr
        report.outcome = "failed"
        report.longrepr = (
            f"a real-browser test skipped where a browser is required (AUTO_BROWSER_REQUIRE_CHROMIUM is set): {reason}"
        )
    return report
