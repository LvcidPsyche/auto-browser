#!/usr/bin/env python
"""Block a release while a privately reported advisory sits unacknowledged.

GHSA-xmh3-cw7j-9gp5 arrived through GitHub private vulnerability reporting on
2026-06-17 and sat in `triage` for seven weeks. Nothing surfaced it: a private
report appears in neither the issue list, the pull-request list, nor anything a
maintainer routinely opens, and it was found only by listing advisories through
the API while publishing an unrelated one. Meanwhile SECURITY.md promises to
"acknowledge reports quickly".

The fix has to attach to a process that is guaranteed to happen. Cutting a
release is the only one, so the check lives in the release audit: a report still
in `triage` means nobody has accepted it, and shipping past that is how seven
weeks happen.

`draft` advisories are reported but do not block — accepted-and-being-fixed is a
legitimate place to be mid-release.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = "LvcidPsyche/auto-browser"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=None, help="owner/name (default: the origin remote)")
    parser.add_argument(
        "--from-json",
        type=Path,
        default=None,
        help="Read the advisory list from a file instead of the API (tests).",
    )
    args = parser.parse_args()

    if args.from_json is not None:
        advisories = json.loads(args.from_json.read_text(encoding="utf-8"))
    else:
        advisories = fetch_advisories(args.repo or resolve_repo())

    return report(advisories, now=datetime.now(timezone.utc))


def resolve_repo() -> str:
    """Derive owner/name from the origin remote so forks check themselves."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return DEFAULT_REPO
    url = result.stdout.strip().removesuffix(".git")
    if url.startswith("git@"):
        url = url.partition(":")[2]
    parts = [part for part in url.split("/") if part]
    return "/".join(parts[-2:]) if len(parts) >= 2 else DEFAULT_REPO


def fetch_advisories(repo: str) -> list[dict]:
    """List repository security advisories via `gh`.

    Raises rather than returning empty on any failure. An advisory check that
    cannot reach the API and reports "nothing open" is indistinguishable from
    success, which is the failure mode this whole script exists to prevent.
    """
    if shutil.which("gh") is None:
        raise SystemExit(
            "Missing required command: gh. The advisory check needs an authenticated "
            "GitHub CLI; pass --skip-advisory-check to the release audit only if you "
            "have just checked the advisory list by hand."
        )
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/security-advisories", "--paginate"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"Could not list security advisories for {repo}:\n{result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def age_days(timestamp: str | None, now: datetime) -> int | None:
    if not timestamp:
        return None
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (now - moment).days


def report(advisories: list[dict], *, now: datetime) -> int:
    unacknowledged = [entry for entry in advisories if entry.get("state") == "triage"]
    drafts = [entry for entry in advisories if entry.get("state") == "draft"]

    for entry in drafts:
        age = age_days(entry.get("created_at"), now)
        suffix = f" ({age}d old)" if age is not None else ""
        print(f"note: {entry.get('ghsa_id')} is an accepted draft{suffix} — publish it with the fix.")

    if not unacknowledged:
        print(f"OK: no security advisory is waiting in triage ({len(advisories)} total).")
        return 0

    print()
    for entry in unacknowledged:
        age = age_days(entry.get("created_at") or entry.get("updated_at"), now)
        suffix = f", waiting {age} days" if age is not None else ""
        print(f"  {entry.get('ghsa_id')} [{entry.get('severity')}]{suffix}")
        print(f"    {entry.get('summary')}")
        print(f"    {entry.get('html_url')}")
    print()
    raise SystemExit(
        f"Release blocked: {len(unacknowledged)} privately reported advisory/advisories "
        "still sit in triage. Accept or close each one before shipping — SECURITY.md "
        "promises reporters a quick acknowledgement."
    )


if __name__ == "__main__":
    raise SystemExit(main())
