"""The bash the tests run the repo's shell scripts with (R5C-27).

On Windows the first bash on PATH can be WSL's launcher (%LOCALAPPDATA%\\Microsoft\\WindowsApps\\bash.exe, or
System32's), which fails before it runs anything of this checkout, or Git's bin\\bash.exe, a wrapper that puts /usr/bin
ahead of a test's stubs on PATH. There a test uses only a bash.exe in a usr\\bin folder (Git's or MSYS2's): the first
on PATH, past any other bash ahead of it, or else the one beside git, or the bash AUTO_BROWSER_TEST_BASH names; with
none, it skips and says why. Elsewhere it is the bash on PATH. A test runs a script with path_after(its stubs) as
PATH, so on Windows what the script runs by name (bash, a stub's ``#!/usr/bin/env bash``, and the tools beside bash:
awk, sed, mktemp, rm) is found beside this bash, after the test's stubs.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

BASH_VARIABLE = "AUTO_BROWSER_TEST_BASH"
NO_BASH_REASON = (
    "needs bash: on Windows, a usr\\bin\\bash.exe (Git's or MSYS2's) on PATH or beside git, or the one "
    f"{BASH_VARIABLE} names"
)


def _in_usr_bin(path: str) -> bool:
    return [part.lower() for part in Path(path).parts][-3:-1] == ["usr", "bin"]


def _usr_bin_bash_on_path() -> str | None:
    """The first bash.exe in a usr\\bin folder on PATH: only the first bash on PATH was looked at, so WSL's launcher
    ahead of MSYS2's, with no git on PATH, left the script tests skipped (review of c5)."""
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(folder) / "bash.exe"
        if folder and _in_usr_bin(str(candidate)) and candidate.is_file():
            return str(candidate)
    return None


def find_bash() -> str | None:
    named = os.environ.get(BASH_VARIABLE)
    if named:
        return named
    if os.name != "nt":
        return shutil.which("bash")
    on_path = _usr_bin_bash_on_path()
    if on_path:
        return on_path
    git = shutil.which("git")
    if git:
        # git.exe sits in Git\cmd, Git\bin or Git\mingw64\bin, depending on the PATH entry.
        for root in Path(git).parents:
            candidate = root / "usr" / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
    return None


BASH = find_bash()
BASH_DIR = str(Path(BASH).parent) if BASH else ""


def path_after(*first: str | Path) -> str:
    """This process's PATH with ``first`` (a test's stubs) ahead of it, and on Windows BASH_DIR after them: there a
    script's bare ``bash``, a stub's ``#!/usr/bin/env bash`` and the tools beside bash found WSL's launcher, or
    nothing, when this bash's folder was not on PATH or came after another bash's (review of c5)."""
    beside_bash = [BASH_DIR] if os.name == "nt" and BASH_DIR else []
    return os.pathsep.join([*map(str, first), *beside_bash, os.environ.get("PATH", "")])
