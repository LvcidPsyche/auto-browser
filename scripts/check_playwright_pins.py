#!/usr/bin/env python3
"""Fail if the controller's Playwright (pip) and browser-node's (npm) cannot work as a pair.

browser-node runs Playwright's server (npm, browser-node/package.json) and the controller connects to it with the
pip Playwright (controller/requirements.txt). The pair must meet two rules, and neither is "one version string".

Rule 1, the server accepts the client. playwright-core's server refuses a client of another major.minor
(browser-node/node_modules/playwright-core/lib/coreBundle.js). In `userAgentVersionMatchesErrorMessage`:

    const received = match[1].split(".").slice(0, 2).join(".");
    const expected = getPlaywrightVersion(true);
    if (received !== expected) {
      return wrapInASCIIBox([
        `Playwright version mismatch:`,

and in `getPlaywrightVersion`, `return majorMinorOnly ? version3.split(".").slice(0, 2).join(".") : version3;`.
That refusal was the #60 outage. With no argument (the lint job) this reads the pins and installs nothing. Inside
each ecosystem the pins agree: controller/requirements.txt and controller/constraints.txt, which the image and the
host tests install under; and browser-node/package.json, which must name an exact version (a range lets npm take
another one), with the playwright and playwright-core of browser-node/package-lock.json, which `npm ci` installs.
Across the two, the major.minor is the same.

Rule 2, both sides run the same browser, which equal version strings do not give: pip 1.65.1 and npm 1.65.1 carry
different Chromium builds, while pip 1.65.2 and npm 1.65.1 carry the same one. With --installed this compares the
`chromium` and `chromium-headless-shell` entries (their revision, browserVersion and revisionOverrides) of the
browsers.json the installed pip driver names (playwright/driver/package/browsers.json) with those of npm's
playwright-core (browser-node/node_modules/playwright-core/browsers.json). A missing file, or an entry whose revision
or browserVersion is missing or not a string, fails, saying what to install; it never passes or skips. What it does
not see: the two bundles may still differ in the switches they launch Chromium with
by default. The product's two copies of Playwright's --disable-features list are held to the pip bundle's by their
own tests (controller/tests/test_playwright_disabled_features.py).

Standard library only, and playwright is never imported: the lint job does not install it.

Exit 0 when the rule holds. Exit 1 when package.json names a range, which the error names, reading no other pin;
when a rule is broken, listing every pin or both sides' builds; and when a browsers.json is missing or lacks an entry.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "controller" / "requirements.txt"
CONSTRAINTS = REPO_ROOT / "controller" / "constraints.txt"
PACKAGE_JSON = REPO_ROOT / "browser-node" / "package.json"
PACKAGE_LOCK = REPO_ROOT / "browser-node" / "package-lock.json"
NODE_MODULES = REPO_ROOT / "browser-node" / "node_modules"

# An exact npm version (a pre-release suffix allowed): no range operator, wildcard or space.
_EXACT_NPM_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
# The browsers.json entries both sides must name alike.
CHROMIUM_ENTRIES = ("chromium", "chromium-headless-shell")
INSTALL_PIP = "install the pinned pip Playwright: `python -m pip install playwright -c controller/constraints.txt`"
INSTALL_NPM = "install browser-node's packages: `npm ci` in browser-node"


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


def _major_minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def check_pins() -> int:
    """Rule 1, from the pins."""
    browser_node = _browser_node_pin()
    if not _EXACT_NPM_VERSION.match(browser_node):
        print(
            f"ERROR: browser-node/package.json pins playwright as {browser_node!r}, a range, not an exact version.\n"
            "npm could install another version than the lock's. Pin the exact version the lock installs.",
            file=sys.stderr,
        )
        return 1

    pip = [
        ("controller/requirements.txt", _pip_pin(REQUIREMENTS)),
        ("controller/constraints.txt", _pip_pin(CONSTRAINTS)),
    ]
    npm = [("browser-node/package.json", browser_node), *_lock_pins()]
    problems = [
        f"the {side} pins are not all the same (inside one ecosystem they must be)"
        for side, pins in (("pip", pip), ("npm", npm))
        if len({version for _, version in pins}) > 1
    ]
    pip_line, npm_line = _major_minor(pip[0][1]), _major_minor(browser_node)
    if pip_line != npm_line:
        problems.append(
            f"pip's Playwright is {pip_line} and npm's {npm_line}: browser-node's server refuses a client of another "
            "major.minor"
        )
    if not problems:
        print(
            f"OK: playwright pinned to {pip[0][1]} for pip and {browser_node} for npm in all {len(pip) + len(npm)} "
            f"places, one major.minor ({pip_line}). `--installed` checks they carry the same Chromium."
        )
        return 0

    print("ERROR: the Playwright pins of the controller and browser-node do not make a pair.", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}.", file=sys.stderr)
    for location, version in (*pip, *npm):
        print(f"  {location:44} {version}", file=sys.stderr)
    print(
        "\nMove both sides together: one version in each ecosystem, the same major.minor across them, and a pair\n"
        "that carries the same Chromium (`python scripts/check_playwright_pins.py --installed`). Then regenerate\n"
        "controller/constraints.txt and run npm install in browser-node. See scripts/check_playwright_pins.py for why.",
        file=sys.stderr,
    )
    return 1


def pip_driver_browsers() -> Path | None:
    """The installed pip Playwright's browsers.json, found without importing playwright: beside the package, where
    Playwright finds its own driver (`playwright._impl._driver.compute_driver_executable`). None if none is installed."""
    spec = importlib.util.find_spec("playwright")
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations))) / "driver" / "package" / "browsers.json"


class _Unreadable(Exception):
    """A browsers.json that is missing, unreadable or lacks an entry: the message says which, and what to install."""


# What names an entry's build. A revision or browserVersion that is missing or not a string is unreadable (two missing
# ones would compare equal), and revisionOverrides installs another build on the platforms it names.
_BUILD_FIELDS = ("revision", "browserVersion")
_OVERRIDES = "revisionOverrides"


def _chromium_builds(path: Path, install: str) -> dict[str, tuple[str, str, object]]:
    """(revision, browserVersion, revisionOverrides or None) of each of CHROMIUM_ENTRIES in the browsers.json at
    `path`."""
    if not path.is_file():
        raise _Unreadable(f"there is no {path}; {install}")
    try:
        entries = {entry["name"]: entry for entry in json.loads(path.read_text(encoding="utf-8"))["browsers"]}
    except (OSError, ValueError, LookupError, TypeError) as exc:
        raise _Unreadable(f"{path} is not a browsers.json Playwright writes ({exc!r}); {install}") from exc
    builds = {}
    for name in CHROMIUM_ENTRIES:
        entry = entries.get(name)
        if entry is None:
            raise _Unreadable(f"{path} has no {name!r} entry")
        for field in _BUILD_FIELDS:
            if not isinstance(entry.get(field), str) or not entry[field]:
                raise _Unreadable(f"{path} has a {name!r} entry without a readable {field} ({entry!r}); {install}")
        builds[name] = (entry["revision"], entry["browserVersion"], entry.get(_OVERRIDES))
    return builds


def _build_text(build: tuple[str, str, object]) -> str:
    revision, version, overrides = build
    text = f"revision {revision} ({version})"
    return text if overrides is None else f"{text} {_OVERRIDES} {json.dumps(overrides, sort_keys=True)}"


def _version_beside(browsers: Path) -> str:
    """The version in the package.json beside a browsers.json: the pip driver's calls itself a beta of its release."""
    try:
        return str(json.loads((browsers.parent / "package.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, LookupError, TypeError) as exc:
        return f"unknown ({exc!r})"


def check_installed(pip_browsers: Path | None, node_modules: Path) -> int:
    """Rule 2, from what is installed."""
    npm_browsers = node_modules / "playwright-core" / "browsers.json"
    try:
        if pip_browsers is None:
            raise _Unreadable(
                f"the pip Playwright is not installed, so there is no browsers.json of its driver; {INSTALL_PIP}"
            )
        pip = _chromium_builds(pip_browsers, INSTALL_PIP)
        npm = _chromium_builds(npm_browsers, INSTALL_NPM)
    except _Unreadable as exc:
        print(f"ERROR: {exc}.", file=sys.stderr)
        return 1

    sides = [("pip driver", pip_browsers, pip), ("npm playwright-core", npm_browsers, npm)]
    described = [
        f"  {side} {_version_beside(path)} ({path}): "
        + ", ".join(f"{name} {_build_text(build)}" for name, build in builds.items())
        for side, path, builds in sides
    ]
    differ = [name for name in CHROMIUM_ENTRIES if pip[name] != npm[name]]
    if not differ:
        print("OK: the pip and the npm Playwright carry the same Chromium.", *described, sep="\n")
        return 0

    print("ERROR: the pip and the npm Playwright carry different Chromium builds.", file=sys.stderr)
    for name in differ:
        print(
            f"  {name}: pip driver {_build_text(pip[name])}, npm playwright-core {_build_text(npm[name])}",
            file=sys.stderr,
        )
    print(*described, sep="\n", file=sys.stderr)
    print(
        "\nPick a pip and an npm release that carry the same build (browsers.json in each), as well as one major.minor.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    """Rule 1 with no arguments (and with argv None, which parses none); rule 2 with --installed."""
    if argv is None:
        return check_pins()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--installed", action="store_true", help="compare the installed packages' Chromium builds")
    parser.add_argument("--pip-browsers", type=Path, help="the pip driver's browsers.json (default: the installed one)")
    parser.add_argument("--node-modules", type=Path, help="browser-node's node_modules (default: the repository's)")
    arguments = parser.parse_args(argv)
    if not arguments.installed:
        if arguments.pip_browsers or arguments.node_modules:
            parser.error("--pip-browsers and --node-modules go with --installed")
        return check_pins()
    pip_browsers = arguments.pip_browsers or pip_driver_browsers()
    return check_installed(pip_browsers, arguments.node_modules or NODE_MODULES)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
