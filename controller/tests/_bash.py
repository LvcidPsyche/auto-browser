"""The bash the tests run the repo's shell scripts with (R5C-27).

On Windows the first bash on PATH can be WSL's launcher (%LOCALAPPDATA%\\Microsoft\\WindowsApps\\bash.exe, or
System32's), which fails before it runs anything of this checkout, or Git's bin\\bash.exe, a wrapper that puts /usr/bin
ahead of a test's stubs on PATH. There a test uses only a bash.exe in a usr\\bin folder (Git's or MSYS2's), the first on
PATH or else the one beside git, or the bash AUTO_BROWSER_TEST_BASH names; with none, it skips and says why. Elsewhere
it is the bash on PATH. A test that puts stubs on PATH puts BASH_DIR after them, so the script finds the tools beside
its bash (awk, sed, mktemp) when that folder is not on PATH, and the test's stubs first.
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


def find_bash() -> str | None:
    named = os.environ.get(BASH_VARIABLE)
    if named:
        return named
    on_path = shutil.which("bash")
    if os.name != "nt" or (on_path and _in_usr_bin(on_path)):
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
