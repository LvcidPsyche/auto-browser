"""Where the real-browser tests find a Chromium to drive.

Each test file used to carry its own copy of a helper that looked only in
`/opt/pw-browsers`, a path that exists in neither CI job nor on a developer
machine, so every real-Chromium test skipped everywhere it ran: the tests that
check what the page actually does, rather than what a mock says it does, never
ran at all. This looks where Playwright installs browsers on each platform, for
the build the installed pip Playwright drives: the newest build in the cache was
another browser as soon as two Playwright versions had been on one machine.
"""

from __future__ import annotations

import glob
import inspect
import json
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

import playwright
import pytest

# Each binary as (the browsers.json entry whose build it is, its path in that build's folder), per platform. Chromium's
# come before the headless shell's: two files tell which they got by "headless-shell" in the path.
_LINUX = (("chromium", "chrome-linux*/chrome"), ("chromium-headless-shell", "chrome-*/chrome-headless-shell"))
_MAC = (("chromium", "chrome-mac*/Chromium.app/Contents/MacOS/Chromium"),)
_WINDOWS = (
    ("chromium", "chrome-win*/chrome.exe"),
    ("chromium-headless-shell", "chrome-headless-shell-win*/chrome-headless-shell.exe"),
)
CHROMIUM_ENTRIES = ("chromium", "chromium-headless-shell")

# The skip reason of `requires_chromium`, by which tests/_real_browser_shards.py tells a real-browser test.
REQUIRES_CHROMIUM_REASON = "no local Chromium binary"


def _browser_roots() -> list[Path]:
    roots = [Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])] if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") else []
    roots += [
        Path("/opt/pw-browsers"),
        Path.home() / ".cache" / "ms-playwright",
        Path.home() / "Library" / "Caches" / "ms-playwright",
    ]
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "ms-playwright")
    return roots


def driver_browsers_json() -> Path:
    """The installed pip Playwright's browsers.json, which names the build of each browser its driver drives. The driver
    is where Playwright finds it (`playwright._impl._driver.compute_driver_executable`)."""
    return Path(inspect.getfile(playwright)).parent / "driver" / "package" / "browsers.json"


def driver_chromium() -> dict[str, dict]:
    """That file's `chromium` and `chromium-headless-shell` entries, by name: none from a file that is missing,
    unreadable or not of Playwright's shape. Then no Chromium is found, and a host test fails
    (test_chromium_requirement.py), so that a Playwright that moves the file turns the host suite red."""
    try:
        entries = json.loads(driver_browsers_json().read_text(encoding="utf-8"))["browsers"]
        return {entry["name"]: entry for entry in entries if entry.get("name") in CHROMIUM_ENTRIES}
    except (OSError, ValueError, LookupError, TypeError, AttributeError):
        return {}


@lru_cache(maxsize=1)
def chromium_executable() -> str | None:
    """A Chromium binary for `chromium.launch(executable_path=...)`, or None.

    AUTO_BROWSER_TEST_CHROMIUM names one explicitly. Otherwise the build the installed driver names, under the first
    Playwright browser root that has it, in the folder Playwright installs it in (`chromium-<revision>`,
    `chromium_headless_shell-<revision>`). Another build is not taken: none is found until
    `python -m playwright install chromium` has fetched this one.
    """
    override = os.environ.get("AUTO_BROWSER_TEST_CHROMIUM")
    if override:
        return override if Path(override).is_file() else None
    revisions = {name: entry.get("revision") for name, entry in driver_chromium().items()}
    for root in _browser_roots():
        for entry, binary in (*_LINUX, *_MAC, *_WINDOWS):
            revision = revisions.get(entry)
            if not isinstance(revision, str) or not revision:
                continue
            # Playwright's folder for a build: the entry's name with "_" for "-", then the revision.
            build = glob.escape(str(root / f"{entry.replace('-', '_')}-{revision}"))
            matches = sorted(glob.glob(os.path.join(build, binary)))
            if matches:
                return matches[-1]
    return None


class ChromiumRequiredError(RuntimeError):
    """AUTO_BROWSER_REQUIRE_CHROMIUM is set and there is no Chromium to drive."""


def chromium_required(environ: Mapping[str, str]) -> bool:
    """Whether the run must have a Chromium (AUTO_BROWSER_REQUIRE_CHROMIUM=1/true/yes)."""
    return environ.get("AUTO_BROWSER_REQUIRE_CHROMIUM", "").strip().lower() in {"1", "true", "yes"}


def requires_chromium_mark(executable: str | None, *, required: bool):
    """The skip mark for the real-browser tests.

    A missing Chromium skips them, unless the run says it must have one: the real-browser-tests
    job sets AUTO_BROWSER_REQUIRE_CHROMIUM, and then a missing browser raises here, as pytest
    starts (tests/conftest.py imports this module), so every run under the switch fails at once
    with the fix in its message instead of going green on skipped tests.
    """
    if executable is None and required:
        raise ChromiumRequiredError(
            "AUTO_BROWSER_REQUIRE_CHROMIUM is set but no Chromium was found (the real-browser tests would "
            "skip); install one with `python -m playwright install chromium` or set AUTO_BROWSER_TEST_CHROMIUM"
        )
    return pytest.mark.skipif(executable is None, reason=REQUIRES_CHROMIUM_REASON)


requires_chromium = requires_chromium_mark(chromium_executable(), required=chromium_required(os.environ))
