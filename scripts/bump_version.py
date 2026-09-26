#!/usr/bin/env python3
"""Set every version string in the repo to one value, then verify parity.

Usage: python scripts/bump_version.py 1.9.0

The locations are the ones ``check_version_parity.py`` enforces (four pyprojects, two
``__version__`` strings, browser-node's package.json and both version fields in its
package-lock.json). Edits are in place and textual, so formatting and key order survive;
each file is re-parsed afterwards and the parity check runs last.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_version_parity as parity  # noqa: E402

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-.]?(?:a|b|rc|dev)\d+)?$")
PYPROJECT_VERSION = re.compile(r'(?m)^(version\s*=\s*")[^"]+(")')
DUNDER = re.compile(r'(?m)^(__version__\s*=\s*")[^"]+(")')
JSON_VERSION = re.compile(r'("version"\s*:\s*")[^"]+(")')
NPM_FILES = (("browser-node/package.json", 1), ("browser-node/package-lock.json", 2))


def replace(path: Path, pattern: re.Pattern, version: str, count: int) -> None:
    with path.open(encoding="utf-8", newline="") as fh:  # keep the file's own line endings
        text = fh.read()
    new, n = pattern.subn(rf"\g<1>{version}\g<2>", text, count=count)
    if n != count:
        raise SystemExit(f"{path}: expected {count} version field(s), found {n}")
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(new)


def bump(version: str, root: Path = parity.REPO_ROOT) -> None:
    for rel in parity.PYPROJECTS:
        replace(root / rel, PYPROJECT_VERSION, version, 1)  # first `version =` is [project]'s
    for rel in parity.DUNDER_VERSIONS:
        replace(root / rel, DUNDER, version, 1)
    for rel, count in NPM_FILES:  # lockfile: top-level version, then packages."".version
        replace(root / rel, JSON_VERSION, version, count)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not SEMVER.match(argv[0]):
        print(__doc__, file=sys.stderr)
        return 2
    bump(argv[0])
    wrong = [(loc, v) for loc, v in parity.collect() if v != argv[0]]
    if wrong:
        for loc, v in wrong:
            print(f"-> {loc}: {v}", file=sys.stderr)
        return 1
    print(f"bumped {len(parity.collect())} version strings to {argv[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
