"""tests/_bash.py: the bash the script tests run on Windows (R5C-27)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._bash import find_bash


@unittest.skipUnless(os.name == "nt", "elsewhere the script tests use the bash on PATH")
class FindBashOnWindowsTests(unittest.TestCase):
    def test_a_usr_bin_bash_after_wsls_launcher_on_path_is_used_with_no_git_there(self) -> None:
        # Review of c5 (Codex): only the first bash on PATH was looked at, so WSL's launcher ahead of MSYS2's
        # usr\bin\bash.exe, with no git on PATH to find Git's beside, left every script test skipped.
        with tempfile.TemporaryDirectory() as tempdir:
            launcher = Path(tempdir, "WindowsApps")
            usr_bin = Path(tempdir, "msys64", "usr", "bin")
            for folder in (launcher, usr_bin):
                folder.mkdir(parents=True)
                (folder / "bash.exe").write_bytes(b"")
            with patch.dict(os.environ, {"PATH": os.pathsep.join([str(launcher), str(usr_bin)])}, clear=True):
                self.assertEqual(find_bash(), str(usr_bin / "bash.exe"))

    def test_with_only_wsls_launcher_there_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            (Path(tempdir) / "bash.exe").write_bytes(b"")
            with patch.dict(os.environ, {"PATH": tempdir}, clear=True):
                self.assertIsNone(find_bash())


if __name__ == "__main__":
    unittest.main()
