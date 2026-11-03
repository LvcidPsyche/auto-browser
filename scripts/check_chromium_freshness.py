#!/usr/bin/env python3
"""Weekly: is the Chromium we ship stale, and has moby's seccomp default moved under our profile?

1. The Chromium that the pinned playwright-core bundles (its browsers.json, read from the exact npm tarball of the
   version browser-node/package-lock.json pins) against Chrome stable for Linux (Google's versionhistory API).
   Fails, naming both versions, when stable's major is ahead, or when a newer playwright-core release bundles a newer
   Chromium (then a Playwright bump would bring security fixes). Otherwise passes and prints the patch gap.
2. controller/app/browser/seccomp/chromium-sandbox.json, minus its auto-browser entry, against moby's default
   profile as the latest Docker Engine release vendors it. The profile replaces the engine's own default, so a stale
   copy could deny a syscall a newer runtime needs: a drift fails, and the fix is refreshing the profile (README.md
   beside it).

Exit 0 when everything is current, 1 on staleness or drift, 2 when a source could not be read.
Standard library only; every fetch is injectable for the offline tests (controller/tests/test_check_chromium_freshness.py).
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_LOCK = REPO_ROOT / "browser-node" / "package-lock.json"
SECCOMP_PROFILE = REPO_ROOT / "controller" / "app" / "browser" / "seccomp" / "chromium-sandbox.json"

NPM_REGISTRY = "https://registry.npmjs.org"
CHROME_STABLE_LINUX = "https://versionhistory.googleapis.com/v1/chrome/platforms/linux/channels/stable/versions"
MOBY_LATEST_RELEASE = "https://api.github.com/repos/moby/moby/releases/latest"
MOBY_DEFAULT_PROFILE = (
    "https://raw.githubusercontent.com/moby/moby/{tag}/vendor/github.com/moby/profiles/seccomp/default.json"
)
AUTO_BROWSER_COMMENT_PREFIX = "auto-browser:"

Fetch = Callable[[str], bytes]


class SourceError(RuntimeError):
    """A source could not be read or did not hold what it should."""


def fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise SourceError(f"refusing a non-https URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "auto-browser-chromium-freshness"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URLs above
            return response.read()
    except OSError as exc:
        raise SourceError(f"could not fetch {url}: {exc}") from exc


def _json(fetcher: Fetch, url: str) -> Any:
    try:
        return json.loads(fetcher(url))
    except ValueError as exc:
        raise SourceError(f"{url} did not return JSON: {exc}") from exc


def version_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError as exc:
        raise SourceError(f"not a dotted version: {version!r}") from exc


def pinned_playwright_core(lock_path: Path = PACKAGE_LOCK) -> str:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    try:
        return lock["packages"]["node_modules/playwright-core"]["version"]
    except KeyError as exc:
        raise SourceError(f"{lock_path} pins no playwright-core") from exc


def bundled_chromium(version: str, fetcher: Fetch) -> str:
    """browserVersion of chromium in the browsers.json of playwright-core <version>, from its npm tarball."""
    meta = _json(fetcher, f"{NPM_REGISTRY}/playwright-core/{version}")
    tarball = meta.get("dist", {}).get("tarball")
    if not tarball:
        raise SourceError(f"npm has no tarball for playwright-core {version}")
    with tarfile.open(fileobj=io.BytesIO(fetcher(tarball)), mode="r:gz") as archive:
        try:
            member = archive.extractfile("package/browsers.json")
        except KeyError as exc:
            raise SourceError(f"playwright-core {version} ships no browsers.json") from exc
        browsers = json.load(member)
    for browser in browsers.get("browsers", []):
        if browser.get("name") == "chromium" and browser.get("browserVersion"):
            return browser["browserVersion"]
    raise SourceError(f"playwright-core {version}'s browsers.json names no chromium browserVersion")


def latest_playwright_core(fetcher: Fetch) -> str:
    version = _json(fetcher, f"{NPM_REGISTRY}/playwright-core/latest").get("version")
    if not version:
        raise SourceError("npm reports no latest playwright-core")
    return version


def chrome_stable(fetcher: Fetch) -> str:
    versions = [entry.get("version", "") for entry in _json(fetcher, CHROME_STABLE_LINUX).get("versions", [])]
    versions = [version for version in versions if version]
    if not versions:
        raise SourceError("versionhistory returned no Chrome stable versions for Linux")
    return max(versions, key=version_tuple)


def evaluate_chromium(
    pinned: str, bundled: str, latest: str, latest_bundled: str, stable: str
) -> tuple[bool, list[str]]:
    """(ok, lines). Stale when stable's major is ahead, or a newer playwright-core bundles a newer Chromium."""
    lines = [
        f"pinned playwright-core {pinned} bundles Chromium {bundled}",
        f"latest playwright-core {latest} bundles Chromium {latest_bundled}",
        f"Chrome stable for Linux: {stable}",
    ]
    ok = True
    if version_tuple(stable)[0] > version_tuple(bundled)[0]:
        ok = False
        lines.append(
            f"STALE: Chrome stable {stable} is a major ahead of the bundled Chromium {bundled} "
            f"(playwright-core {pinned}); its security fixes are not in the browser we ship."
        )
    if version_tuple(latest) > version_tuple(pinned) and version_tuple(latest_bundled) > version_tuple(bundled):
        ok = False
        lines.append(
            f"STALE: playwright-core {latest} bundles Chromium {latest_bundled}, newer than the Chromium {bundled} "
            f"of the pinned {pinned}; bump Playwright on both sides (scripts/check_playwright_pins.py)."
        )
    if ok:
        lines.append(f"OK: {_patch_gap(bundled, stable)}")
    return ok, lines


def _patch_gap(bundled: str, stable: str) -> str:
    b, s = version_tuple(bundled), version_tuple(stable)
    if b == s:
        return f"the bundled Chromium {bundled} is Chrome stable."
    if b > s:
        return f"the bundled Chromium {bundled} is newer than Chrome stable {stable}."
    if b[:3] == s[:3]:
        return f"same build {'.'.join(map(str, b[:3]))}; stable is {s[3] - b[3]} patch number(s) ahead ({stable} vs {bundled})."
    return f"same major {b[0]}; stable {stable} is a later build than the bundled {bundled}."


def strip_auto_browser(profile: dict[str, Any]) -> dict[str, Any]:
    stripped = dict(profile)
    stripped["syscalls"] = [
        entry
        for entry in profile.get("syscalls", [])
        if not str(entry.get("comment", "")).startswith(AUTO_BROWSER_COMMENT_PREFIX)
    ]
    return stripped


def profile_drift(shipped: dict[str, Any], upstream: dict[str, Any]) -> list[str]:
    """How the shipped profile, minus the auto-browser entry, differs from upstream (empty when it does not)."""
    ours = strip_auto_browser(shipped)
    differences = []
    for key in sorted(set(ours) | set(upstream)):
        if key != "syscalls" and ours.get(key) != upstream.get(key):
            differences.append(f"top-level {key!r} differs")
    mine, theirs = ours.get("syscalls", []), upstream.get("syscalls", [])
    if len(mine) == len(theirs):
        # Upstream usually edits entries in place: name what changed in each, syscall by syscall.
        for index, (old, new) in enumerate(zip(mine, theirs)):
            if old == new:
                continue
            added = sorted(set(new.get("names", [])) - set(old.get("names", [])))
            removed = sorted(set(old.get("names", [])) - set(new.get("names", [])))
            fields = sorted(key for key in set(old) | set(new) if key != "names" and old.get(key) != new.get(key))
            parts = [f"syscalls[{index}] ({_entry_label(new)})"]
            if added:
                parts.append(f"upstream adds {', '.join(added)}")
            if removed:
                parts.append(f"upstream removes {', '.join(removed)}")
            if fields:
                parts.append(f"changes {', '.join(fields)}")
            differences.append(": ".join(parts[:1]) + ": " + "; ".join(parts[1:] or ["reordered names"]))
        return differences
    as_text = lambda entries: [json.dumps(entry, sort_keys=True) for entry in entries]  # noqa: E731
    for entry, text in zip(theirs, as_text(theirs)):
        if text not in as_text(mine):
            differences.append(f"upstream has an entry the profile lacks ({_entry_label(entry)})")
    for entry, text in zip(mine, as_text(mine)):
        if text not in as_text(theirs):
            differences.append(f"the profile has an entry upstream dropped or changed ({_entry_label(entry)})")
    return differences


def _entry_label(entry: dict[str, Any]) -> str:
    names = entry.get("names", [])
    shown = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
    return f"{entry.get('action', '?')} {shown}"


def latest_moby_default(fetcher: Fetch) -> tuple[str, dict[str, Any]]:
    tag = _json(fetcher, MOBY_LATEST_RELEASE).get("tag_name")
    if not tag:
        raise SourceError("GitHub reports no latest moby release")
    return tag, _json(fetcher, MOBY_DEFAULT_PROFILE.format(tag=tag))


def run(fetcher: Fetch = fetch, lock_path: Path = PACKAGE_LOCK, profile_path: Path = SECCOMP_PROFILE) -> int:
    try:
        pinned = pinned_playwright_core(lock_path)
        bundled = bundled_chromium(pinned, fetcher)
        latest = latest_playwright_core(fetcher)
        latest_bundled = bundled if latest == pinned else bundled_chromium(latest, fetcher)
        stable = chrome_stable(fetcher)
        tag, upstream = latest_moby_default(fetcher)
    except SourceError as exc:
        print(f"ERROR: {exc}")
        return 2
    chromium_ok, lines = evaluate_chromium(pinned, bundled, latest, latest_bundled, stable)
    print("\n".join(lines))

    drift = profile_drift(json.loads(profile_path.read_text(encoding="utf-8")), upstream)
    if drift:
        print(
            f"DRIFT: {profile_path.relative_to(REPO_ROOT) if profile_path.is_relative_to(REPO_ROOT) else profile_path}"
        )
        print(f"  minus its auto-browser entry, it no longer matches moby's default profile as {tag} vendors it:")
        for line in drift:
            print(f"  - {line}")
        print("  Refresh it (controller/app/browser/seccomp/README.md).")
    else:
        print(f"OK: the seccomp profile minus its auto-browser entry matches moby's default as {tag} vendors it.")
    return 0 if chromium_ok and not drift else 1


if __name__ == "__main__":
    sys.exit(run())
