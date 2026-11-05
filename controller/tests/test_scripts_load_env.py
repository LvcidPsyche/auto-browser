"""load_repo_env reads KEY=VALUE lines from .env; it never executes them.

It used to `source` the file, so `X=$(touch marker)` in a .env ran the command, and an
unquoted `&`, `;` or `(` in a value broke or hijacked every script that loads it (doctor,
bootstrap_cli_auth, compose_local and the smoke scripts). It now parses the lines itself.

The tests run the real function in bash against a scratch file in a temp directory, never
the repo's .env. Repo-layout checks: skipped where scripts/ or bash is absent (the
controller image).
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_scripts_token_argv import BASH, SCRIPTS, _bash_path


@unittest.skipUnless(SCRIPTS.is_dir() and BASH, "scripts/ or bash is not available here")
class LoadRepoEnvTests(unittest.TestCase):
    def load(self, env_text: str, *, extra: str = "") -> tuple[dict[str, str], Path]:
        """The variables exported to a child after load_repo_env, and the temp dir (for markers)."""
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        temp = Path(tempdir.name)
        env_file = temp / "scratch.env"
        env_file.write_bytes(env_text.replace("{MARKER}", _bash_path(temp / "marker")).encode())
        script = (
            f'source "{_bash_path(SCRIPTS / "load_env.sh")}"; '
            f'load_repo_env "{_bash_path(env_file)}"; {extra} '
            "bash -c 'env'"
        )
        # Bytes, decoded by hand: text mode would turn a stray carriage return into a newline.
        result = subprocess.run([BASH, "-c", script], capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        stdout = result.stdout.decode("utf-8")
        exported = dict(line.split("=", 1) for line in stdout.split("\n") if "=" in line)
        return exported, temp

    def test_a_command_substitution_in_a_value_is_not_run(self) -> None:
        exported, temp = self.load("SAFE=1\nX=$(touch {MARKER})\nY=`touch {MARKER}`\nZ=a;touch {MARKER}\n")

        self.assertFalse((temp / "marker").exists(), "the .env line was executed")
        self.assertEqual(exported["SAFE"], "1")
        self.assertTrue(exported["X"].startswith("$(touch "), exported["X"])
        self.assertTrue(exported["Y"].startswith("`touch "), exported["Y"])
        self.assertTrue(exported["Z"].startswith("a;touch "), exported["Z"])

    def test_values_keep_equals_signs_and_shell_characters(self) -> None:
        exported, _ = self.load("URL=postgres://u:p@h/db?a=b=c\nAMP=a&b|c(d)\nPATH_LIKE=$HOME/x\n")

        self.assertEqual(exported["URL"], "postgres://u:p@h/db?a=b=c")
        self.assertEqual(exported["AMP"], "a&b|c(d)")
        self.assertEqual(exported["PATH_LIKE"], "$HOME/x")

    def test_one_level_of_matching_quotes_is_stripped(self) -> None:
        exported, _ = self.load(
            'A="hello world"\nB=\'single $HOME\'\nC="it\'s"\nD="a # b"\nE="mismatched\nF=\'"nested"\'\n'
        )

        self.assertEqual(exported["A"], "hello world")
        self.assertEqual(exported["B"], "single $HOME")
        self.assertEqual(exported["C"], "it's")
        self.assertEqual(exported["D"], "a # b")
        self.assertEqual(exported["E"], '"mismatched')
        self.assertEqual(exported["F"], '"nested"')

    def test_comments_blanks_export_prefix_and_crlf(self) -> None:
        exported, _ = self.load(
            "# a comment\n\n   \n  INDENT=1\nexport EXPORTED=2\nLOG=INFO  # trailing note\nWIN=3\r\nEMPTY=\nnot a pair\n9BAD=x\n"
        )

        self.assertEqual(exported["INDENT"], "1")
        self.assertEqual(exported["EXPORTED"], "2")
        self.assertEqual(exported["LOG"], "INFO")
        self.assertEqual(exported["WIN"], "3")
        self.assertEqual(exported["EMPTY"], "")
        self.assertNotIn("9BAD", exported)

    def test_a_last_line_without_a_newline_is_read(self) -> None:
        exported, _ = self.load("FIRST=1\nLAST=2")

        self.assertEqual(exported["LAST"], "2")

    def test_a_missing_file_is_not_an_error(self) -> None:
        result = subprocess.run(
            [BASH, "-c", f'source "{_bash_path(SCRIPTS / "load_env.sh")}"; load_repo_env /no/such/file; echo done'],
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual((result.returncode, result.stdout.strip()), (0, "done"))


if __name__ == "__main__":
    unittest.main()
