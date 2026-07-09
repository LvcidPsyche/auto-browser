#!/usr/bin/env python3
"""Fail if the Playwright pins in the controller and browser-node drift apart.

The controller (Python, controller/requirements.txt) and the browser-node
(Node, browser-node/package.json) run Playwright over the same CDP protocol.
A version mismatch between them has broken CI before (#76), so this guard makes
the matched-pair invariant machine-enforced instead of tribal knowledge: any
single-side bump (e.g. a Dependabot PR touching only one) fails here until the
other side is bumped to match.

Exit 0 when the pins match, 1 (with a diff) when they don't.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "controller" / "requirements.txt"
PACKAGE_JSON = REPO_ROOT / "browser-node" / "package.json"

# Strip a leading npm range operator (^, ~, >=, <=, >, <, =) so "^1.61.0" and
# "1.61.0" compare equal. The pins are expected to be exact, but tolerate ranges.
_RANGE_PREFIX = re.compile(r"^[\^~=><]+")


def _controller_pin() -> str:
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        # Matches "playwright==1.61.0" and "playwright[extra]==1.61.0".
        match = re.match(r"^playwright(?:\[[^\]]*\])?==([^\s;]+)", line)
        if match:
            return match.group(1)
    raise SystemExit(f"could not find a pinned 'playwright==' in {REQUIREMENTS}")


def _browser_node_pin() -> str:
    data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        version = data.get(section, {}).get("playwright")
        if version:
            return _RANGE_PREFIX.sub("", version.strip())
    raise SystemExit(f"could not find a 'playwright' dependency in {PACKAGE_JSON}")


def main() -> int:
    controller = _controller_pin()
    browser_node = _browser_node_pin()
    if controller == browser_node:
        print(f"OK: playwright pinned to {controller} in both controller and browser-node.")
        return 0

    print(
        "ERROR: Playwright version mismatch between controller and browser-node.\n"
        f"  controller/requirements.txt : playwright=={controller}\n"
        f"  browser-node/package.json   : playwright  {browser_node}\n"
        "\nThey must move as a matched pair (they share the CDP protocol). Bump both\n"
        "to the same version. See scripts/check_playwright_pins.py for why.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
