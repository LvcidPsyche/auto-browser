#!/usr/bin/env python3
"""Fail if the Playwright pins in the controller and browser-node drift apart.

The controller (Python, controller/requirements.txt) and the browser-node
(Node, browser-node/package.json) run Playwright over the same CDP protocol.
A version mismatch between them has broken CI before (#76), so this guard makes
the matched-pair invariant machine-enforced instead of tribal knowledge: any
single-side bump (e.g. a Dependabot PR touching only one) fails here until the
other side is bumped to match.

What is installed is read too: controller/constraints.txt, which the image and
the host tests install under, and browser-node/package-lock.json, which
`npm ci` installs from (playwright and the playwright-core it runs). And
package.json must name an exact version: a range lets npm take another one.

Exit 0 when the pins match. Exit 1 when package.json names a range, which the
error names, reading no other pin; and 1 when the pins differ, listing every pin.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "controller" / "requirements.txt"
CONSTRAINTS = REPO_ROOT / "controller" / "constraints.txt"
PACKAGE_JSON = REPO_ROOT / "browser-node" / "package.json"
PACKAGE_LOCK = REPO_ROOT / "browser-node" / "package-lock.json"

# An exact npm version (a pre-release suffix allowed): no range operator, wildcard or space.
_EXACT_NPM_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def _pip_pin(path: Path) -> str:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        # Matches "playwright==1.61.0" and "playwright[extra]==1.61.0".
        match = re.match(r"^playwright(?:\[[^\]]*\])?==([^\s;]+)", line)
        if match:
            return match.group(1)
    raise SystemExit(f"could not find a pinned 'playwright==' in {path}")


def _browser_node_pin() -> str:
    data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        version = data.get(section, {}).get("playwright")
        if version:
            return version.strip()
    raise SystemExit(f"could not find a 'playwright' dependency in {PACKAGE_JSON}")


def _lock_pins() -> list[tuple[str, str]]:
    packages = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8")).get("packages", {})
    found = []
    for name in ("playwright", "playwright-core"):
        version = packages.get(f"node_modules/{name}", {}).get("version")
        if not version:
            raise SystemExit(f"could not find node_modules/{name} in {PACKAGE_LOCK}")
        found.append((f"browser-node/package-lock.json ({name})", version))
    return found


def main() -> int:
    browser_node = _browser_node_pin()
    if not _EXACT_NPM_VERSION.match(browser_node):
        print(
            f"ERROR: browser-node/package.json pins playwright as {browser_node!r}, a range, not an exact version.\n"
            "npm could install another version than the controller's. Pin the exact version both sides use.",
            file=sys.stderr,
        )
        return 1

    pins = [
        ("controller/requirements.txt", _pip_pin(REQUIREMENTS)),
        ("controller/constraints.txt", _pip_pin(CONSTRAINTS)),
        ("browser-node/package.json", browser_node),
        *_lock_pins(),
    ]
    if len({version for _, version in pins}) == 1:
        print(f"OK: playwright pinned to {browser_node} in all {len(pins)} places, controller and browser-node.")
        return 0

    print("ERROR: Playwright version mismatch between controller and browser-node.", file=sys.stderr)
    for location, version in pins:
        print(f"  {location:44} {version}", file=sys.stderr)
    print(
        "\nThey must move as a matched pair (they share the CDP protocol). Bump both\n"
        "to the same version, regenerate controller/constraints.txt and run npm install\n"
        "in browser-node. See scripts/check_playwright_pins.py for why.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
