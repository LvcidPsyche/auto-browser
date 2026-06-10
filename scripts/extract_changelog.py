#!/usr/bin/env python3
"""Print the CHANGELOG.md section for one version (release-notes helper).

Usage: python scripts/extract_changelog.py 1.2.1 [CHANGELOG.md]
Exits non-zero if the version has no section, so a release with missing
notes fails loudly instead of publishing empty.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def extract(version: str, changelog_text: str) -> str | None:
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)",
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(changelog_text)
    if match is None:
        return None
    return match.group(1).strip()


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: extract_changelog.py <version> [changelog-path]", file=sys.stderr)
        return 2
    version = sys.argv[1].lstrip("v")
    path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parents[1] / "CHANGELOG.md"
    section = extract(version, path.read_text(encoding="utf-8"))
    if not section:
        print(f"No CHANGELOG section found for version {version}", file=sys.stderr)
        return 1
    print(section)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
