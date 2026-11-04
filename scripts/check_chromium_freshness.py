#!/usr/bin/env python3
"""Weekly: is the Chromium we ship stale, and has moby's seccomp default moved under our profile?

1. The Chromium that the pinned playwright-core bundles (its browsers.json, read from the exact npm tarball of the
   version browser-node/package-lock.json pins) against Chrome stable for Linux (Google's versionhistory API).
   Fails, naming both versions, when stable's major is ahead, or when a newer playwright-core release bundles a newer
   Chromium (then a Playwright bump would bring security fixes). Otherwise passes and prints the patch gap; a
   same-build gap above PATCH_GAP_WARNING is a GitHub warning annotation and a job-summary line (stable patch
   releases carry Chrome's security fixes), still exit 0 since no Playwright release fixes it.
2. controller/app/browser/seccomp/chromium-sandbox.json, minus its auto-browser entry, against moby's default
   profile as the latest Docker Engine release vendors it. The profile replaces the engine's own default, so a stale
   copy could deny a syscall a newer runtime needs: a drift fails, and the fix is refreshing the profile (README.md
   beside it).

Exit 0 when everything is current, 1 on staleness or drift, 2 (with an ERROR line, never a traceback) when a source
could not be read or held something unexpected. GITHUB_TOKEN, when set, is sent to api.github.com only.
Standard library only; every fetch is injectable for the offline tests (controller/tests/test_check_chromium_freshness.py).
"""

from __future__ import annotations

import http.client
import io
import json
import os
import sys
import tarfile
import urllib.parse
import urllib.request
from collections import defaultdict
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
PATCH_GAP_WARNING = 20
_GITHUB_API_HOST = "api.github.com"

Fetch = Callable[[str], bytes]


class SourceError(RuntimeError):
    """A source could not be read or did not hold what it should."""


class GitHubTokenRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib copies a request's headers onto its redirect: keep Authorization only for https://api.github.com."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        target = urllib.parse.urlsplit(newurl)
        if new is not None and (target.scheme != "https" or target.hostname != _GITHUB_API_HOST):
            new.remove_header("Authorization")
        return new


def fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise SourceError(f"refusing a non-https URL: {url}")
    headers = {"User-Agent": "auto-browser-chromium-freshness"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlsplit(url).hostname == _GITHUB_API_HOST:
        # Unauthenticated, api.github.com allows 60 requests an hour per IP, shared across a hosted runner pool.
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    try:
        opener = urllib.request.build_opener(GitHubTokenRedirectHandler)
        with opener.open(request, timeout=60) as response:  # noqa: S310 - fixed https URLs above
            return response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise SourceError(f"could not fetch {url}: {exc!r}") from exc


def _json(fetcher: Fetch, url: str, kind: type = dict) -> Any:
    try:
        value = json.loads(fetcher(url))
    except ValueError as exc:
        raise SourceError(f"{url} did not return JSON: {exc}") from exc
    if not isinstance(value, kind):
        raise SourceError(f"{url} returned a JSON {type(value).__name__}, not a {kind.__name__}")
    return value


def version_tuple(version: Any) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(version).split("."))
    except ValueError as exc:
        raise SourceError(f"not a dotted version: {version!r}") from exc


def pinned_playwright_core(lock_path: Path = PACKAGE_LOCK) -> str:
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        version = lock["packages"]["node_modules/playwright-core"]["version"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"{lock_path} pins no playwright-core ({exc!r})") from exc
    version_tuple(version)  # a prerelease or odd pin is an error here, not a traceback later
    return str(version)


def bundled_chromium(version: str, fetcher: Fetch) -> str:
    """browserVersion of chromium in the browsers.json of playwright-core <version>, from its npm tarball."""
    meta = _json(fetcher, f"{NPM_REGISTRY}/playwright-core/{version}")
    dist = meta.get("dist")
    tarball = dist.get("tarball") if isinstance(dist, dict) else None
    if not isinstance(tarball, str) or not tarball:
        raise SourceError(f"npm has no tarball for playwright-core {version}")
    try:
        with tarfile.open(fileobj=io.BytesIO(fetcher(tarball)), mode="r:gz") as archive:
            member = archive.extractfile("package/browsers.json")
            if member is None:
                raise SourceError(f"playwright-core {version}'s browsers.json is not a file")
            browsers = json.load(member)
    except KeyError as exc:
        raise SourceError(f"playwright-core {version} ships no browsers.json") from exc
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        raise SourceError(f"playwright-core {version}'s tarball could not be read: {exc!r}") from exc
    entries = browsers.get("browsers") if isinstance(browsers, dict) else None
    for browser in entries if isinstance(entries, list) else []:
        if isinstance(browser, dict) and browser.get("name") == "chromium" and browser.get("browserVersion"):
            version_tuple(browser["browserVersion"])
            return str(browser["browserVersion"])
    raise SourceError(f"playwright-core {version}'s browsers.json names no chromium browserVersion")


def latest_playwright_core(fetcher: Fetch) -> str:
    version = _json(fetcher, f"{NPM_REGISTRY}/playwright-core/latest").get("version")
    if not isinstance(version, str) or not version:
        raise SourceError("npm reports no latest playwright-core")
    version_tuple(version)
    return version


def chrome_stable(fetcher: Fetch) -> str:
    entries = _json(fetcher, CHROME_STABLE_LINUX).get("versions")
    if not isinstance(entries, list):
        raise SourceError("versionhistory returned no list of Chrome stable versions for Linux")
    versions = [entry.get("version") for entry in entries if isinstance(entry, dict)]
    versions = [version for version in versions if isinstance(version, str) and version]
    if not versions:
        raise SourceError("versionhistory returned no Chrome stable versions for Linux")
    return max(versions, key=version_tuple)


def evaluate_chromium(
    pinned: str, bundled: str, latest: str, latest_bundled: str, stable: str
) -> tuple[bool, list[str], str | None]:
    """(ok, lines, warning). Stale when stable's major is ahead, or a newer playwright-core bundles a newer
    Chromium. warning: a same-build patch gap above PATCH_GAP_WARNING, which no Playwright release fixes yet."""
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
    warning = None
    if ok:
        gap_text, gap = _patch_gap(bundled, stable)
        lines.append(f"OK: {gap_text}")
        if gap is not None and gap > PATCH_GAP_WARNING:
            warning = (
                f"The bundled Chromium {bundled} is {gap} patch numbers behind Chrome stable {stable} on the same "
                f"build; no Playwright release bundles a newer one yet."
            )
        b, st = version_tuple(bundled), version_tuple(stable)
        if warning is None and b[:1] == st[:1] and len(b) == len(st) == 4 and st[:3] > b[:3]:
            # D17: the same major, a later build: its fixes are not in the browser we ship either.
            warning = (
                f"Chrome stable {stable} is a later build than the bundled Chromium {bundled} on the same major; no "
                "Playwright release bundles it yet."
            )
    return ok, lines, warning


def _patch_gap(bundled: str, stable: str) -> tuple[str, int | None]:
    b, s = version_tuple(bundled), version_tuple(stable)
    if b == s:
        return f"the bundled Chromium {bundled} is Chrome stable.", 0
    if b > s:
        return f"the bundled Chromium {bundled} is newer than Chrome stable {stable}.", None
    if len(b) == len(s) == 4 and b[:3] == s[:3]:
        gap = s[3] - b[3]
        return (
            f"same build {'.'.join(map(str, b[:3]))}; stable is {gap} patch number(s) ahead ({stable} vs {bundled}).",
            gap,
        )
    return f"same major {b[0]}; stable {stable} is a later build than the bundled {bundled}.", None


def strip_auto_browser(profile: dict[str, Any]) -> dict[str, Any]:
    stripped = dict(profile)
    stripped["syscalls"] = [
        entry
        for entry in profile.get("syscalls", [])
        if not str(entry.get("comment", "")).startswith(AUTO_BROWSER_COMMENT_PREFIX)
    ]
    return stripped


def _condition(entry: dict[str, Any]) -> str:
    """What distinguishes one entry for a syscall from another: its action, arg filters and cap/arch conditions."""
    parts = [str(entry.get("action", "?"))]
    ops = {
        "SCMP_CMP_EQ": "==",
        "SCMP_CMP_NE": "!=",
        "SCMP_CMP_MASKED_EQ": "&=",
        "SCMP_CMP_GE": ">=",
        "SCMP_CMP_GT": ">",
        "SCMP_CMP_LE": "<=",
        "SCMP_CMP_LT": "<",
    }
    for arg in entry.get("args") or []:
        if isinstance(arg, dict):
            parts.append(f"arg{arg.get('index')} {ops.get(arg.get('op'), arg.get('op'))} {arg.get('value')}")
    for key in ("includes", "excludes"):
        if entry.get(key):
            parts.append(f"{key} {json.dumps(entry[key], sort_keys=True)}")
    return ", ".join(parts)


def profile_drift(shipped: dict[str, Any], upstream: dict[str, Any]) -> list[str]:
    """How the shipped profile, minus the auto-browser entry, differs from upstream (empty when it does not).

    Grouped by syscall: one line per syscall whose rules differ, with the distinguishing conditions on each side."""
    ours = strip_auto_browser(shipped)
    differences = [
        f"top-level {key!r} differs"
        for key in sorted(set(ours) | set(upstream))
        if key != "syscalls" and ours.get(key) != upstream.get(key)
    ]

    def by_name(profile: dict[str, Any]) -> dict[str, list[str]]:
        rules: dict[str, list[str]] = defaultdict(list)
        for entry in profile.get("syscalls", []):
            for name in entry.get("names", []):
                rules[name].append(_condition(entry))
        return rules

    mine, theirs = by_name(ours), by_name(upstream)
    for name in sorted(set(mine) | set(theirs)):
        if sorted(mine.get(name, [])) == sorted(theirs.get(name, [])):
            continue
        ours_text = "; ".join(sorted(mine.get(name, []))) or "none"
        theirs_rules = sorted(theirs.get(name, []))
        differences.append(
            f"{name}: profile has [{ours_text}]; {len(theirs_rules)} upstream entries [{'; '.join(theirs_rules) or 'none'}]"
        )
    if not differences and ours.get("syscalls") != upstream.get("syscalls"):
        differences.append("syscalls entries are the same but in a different order")
    return differences


def latest_moby_default(fetcher: Fetch) -> tuple[str, dict[str, Any]]:
    tag = _json(fetcher, MOBY_LATEST_RELEASE).get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise SourceError("GitHub reports no latest moby release")
    profile = _json(fetcher, MOBY_DEFAULT_PROFILE.format(tag=tag))
    if not isinstance(profile.get("syscalls"), list) or not isinstance(profile.get("defaultAction"), str):
        raise SourceError(f"moby {tag}'s default.json is not a seccomp profile (no syscalls list or defaultAction)")
    return tag, profile


def _summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            print(f"WARNING: could not write the job summary to {path}: {exc.strerror or exc}")


def run(fetcher: Fetch = fetch, lock_path: Path = PACKAGE_LOCK, profile_path: Path = SECCOMP_PROFILE) -> int:
    try:
        pinned = pinned_playwright_core(lock_path)
        bundled = bundled_chromium(pinned, fetcher)
        latest = latest_playwright_core(fetcher)
        latest_bundled = bundled if latest == pinned else bundled_chromium(latest, fetcher)
        stable = chrome_stable(fetcher)
        tag, upstream = latest_moby_default(fetcher)
        chromium_ok, lines, warning = evaluate_chromium(pinned, bundled, latest, latest_bundled, stable)
        shipped = json.loads(profile_path.read_text(encoding="utf-8"))
        drift = profile_drift(shipped, upstream)
    except SourceError as exc:
        print(f"ERROR: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - every anomaly is exit 2 with a line, never a traceback
        print(f"ERROR: unexpected {type(exc).__name__}: {exc}")
        return 2
    print("\n".join(lines))
    if warning:
        print(f"::warning::{warning}")
        _summary(f"- {warning}")

    if drift:
        shown = profile_path.relative_to(REPO_ROOT) if profile_path.is_relative_to(REPO_ROOT) else profile_path
        print(f"DRIFT: {shown}")
        print(f"  minus its auto-browser entry, it no longer matches moby's default profile as {tag} vendors it:")
        for line in drift:
            print(f"  - {line}")
        print("  Refresh it (controller/app/browser/seccomp/README.md).")
    else:
        print(f"OK: the seccomp profile minus its auto-browser entry matches moby's default as {tag} vendors it.")
    return 0 if chromium_ok and not drift else 1


if __name__ == "__main__":
    sys.exit(run())
