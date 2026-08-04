#!/usr/bin/env python3
"""Fail if the version strings across the repo's packages drift apart.

The release workflow verifies the tag against the three *published* pyproject
files only. Everything else — the controller's runtime version, the client's
``__version__``, browser-node's package files — is unchecked, and that gap has
bitten before: v1.4.0 shipped with the four pyprojects bumped while the runtime
strings sat at 1.3.1, needing a follow-up alignment commit (0e41f95).

This makes the matched-set invariant machine-enforced instead of a checklist
someone has to remember during a release. Every location below must agree.

Exit 0 when all versions match, 1 (listing every offender) when they don't.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PYPROJECTS = (
    "client/pyproject.toml",
    "controller/pyproject.toml",
    "integrations/langchain/pyproject.toml",
    "packaging/auto-browser-mcp/pyproject.toml",
)

DUNDER_VERSIONS = (
    "controller/app/version.py",
    "client/auto_browser_client/__init__.py",
)

_DUNDER = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def _pyproject_version(relative: str) -> tuple[str, str]:
    path = REPO_ROOT / relative
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return relative, data["project"]["version"]


def _dunder_version(relative: str) -> tuple[str, str]:
    path = REPO_ROOT / relative
    match = _DUNDER.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit(f"could not find a '__version__ = \"...\"' in {relative}")
    return relative, match.group(1)


def _npm_versions() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    package = json.loads((REPO_ROOT / "browser-node/package.json").read_text(encoding="utf-8"))
    found.append(("browser-node/package.json", package["version"]))

    lock = json.loads((REPO_ROOT / "browser-node/package-lock.json").read_text(encoding="utf-8"))
    found.append(("browser-node/package-lock.json", lock["version"]))
    # The lockfile repeats the version on its root package entry; npm rewrites
    # both, so a hand-edit that misses one is exactly the drift worth catching.
    root_entry = lock.get("packages", {}).get("")
    if isinstance(root_entry, dict) and "version" in root_entry:
        found.append(('browser-node/package-lock.json (packages."")', root_entry["version"]))

    return found


def collect() -> list[tuple[str, str]]:
    versions = [_pyproject_version(path) for path in PYPROJECTS]
    versions += [_dunder_version(path) for path in DUNDER_VERSIONS]
    versions += _npm_versions()
    return versions


def main() -> int:
    versions = collect()
    distinct = {version for _, version in versions}

    if len(distinct) == 1:
        print(f"OK: all {len(versions)} version strings agree on {versions[0][1]}.")
        return 0

    # Report against the most common value so the odd ones out are obvious.
    counts = {value: sum(1 for _, v in versions if v == value) for value in distinct}
    expected = max(counts, key=lambda value: counts[value])

    print(
        "ERROR: version strings have drifted apart.\n"
        f"  most common: {expected} ({counts[expected]} of {len(versions)})\n",
        file=sys.stderr,
    )
    for location, version in versions:
        marker = "  " if version == expected else "->"
        print(f"{marker} {location:52} {version}", file=sys.stderr)
    print(
        "\nAll of these must move together in a release commit. See scripts/check_version_parity.py for why.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
