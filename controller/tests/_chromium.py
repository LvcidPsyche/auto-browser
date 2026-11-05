"""Where the real-browser tests find a Chromium to drive.

Each test file used to carry its own copy of a helper that looked only in
`/opt/pw-browsers`, a path that exists in neither CI job nor on a developer
machine, so every real-Chromium test skipped everywhere it ran: the tests that
check what the page actually does, rather than what a mock says it does, never
ran at all. This looks where Playwright installs browsers on each platform.
"""

from __future__ import annotations

import glob
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

import pytest

_LINUX = ("chromium-*/chrome-linux*/chrome", "chromium_headless_shell-*/chrome-*/chrome-headless-shell")
_MAC = ("chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",)
_WINDOWS = (
    "chromium-*/chrome-win*/chrome.exe",
    "chromium_headless_shell-*/chrome-headless-shell-win*/chrome-headless-shell.exe",
)


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


@lru_cache(maxsize=1)
def chromium_executable() -> str | None:
    """A Chromium binary for `chromium.launch(executable_path=...)`, or None.

    AUTO_BROWSER_TEST_CHROMIUM names one explicitly. Otherwise the newest build
    under any Playwright browser root wins.
    """
    override = os.environ.get("AUTO_BROWSER_TEST_CHROMIUM")
    if override:
        return override if Path(override).is_file() else None
    for root in _browser_roots():
        for pattern in (*_LINUX, *_MAC, *_WINDOWS):
            matches = sorted(glob.glob(str(root / pattern)))
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

    A missing Chromium skips them, unless the run says it must have one: a CI leg that
    installs Chromium sets AUTO_BROWSER_REQUIRE_CHROMIUM, and then a missing browser raises
    here, at collection, so the run fails instead of going green on skipped tests.
    """
    if executable is None and required:
        raise ChromiumRequiredError(
            "AUTO_BROWSER_REQUIRE_CHROMIUM is set but no Chromium was found (the real-browser tests would "
            "skip); install one with `python -m playwright install chromium` or set AUTO_BROWSER_TEST_CHROMIUM"
        )
    return pytest.mark.skipif(executable is None, reason="no local Chromium binary")


requires_chromium = requires_chromium_mark(chromium_executable(), required=chromium_required(os.environ))
