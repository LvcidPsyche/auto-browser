"""load_repo_env reads KEY=VALUE lines from .env; it never executes them.

It used to `source` the file, so `X=$(touch marker)` in a .env ran the command, and an
unquoted `&`, `;` or `(` in a value broke or hijacked every script that loads it (doctor,
bootstrap_cli_auth, compose_local and the smoke scripts). It now parses the lines itself,
refuses the keys that change how the shell or the loader behaves, and warns (key name or
line number only, never a value) about every line it skips.

The tests run the real function in bash against a scratch file in a temp directory, never
the repo's .env, in a child with a minimal environment that prints only the keys under test.
Repo-layout checks: skipped where scripts/ or bash is absent (the controller image).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_scripts_token_argv import BASH, SCRIPTS, _bash_path

KEY_RE = re.compile(r"^[ \t﻿]*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", re.M)
# A child bash prints "KEY=value" NUL-terminated for each named variable that is set and exported.
PRINT_KEYS = r"""bash -c 'for k in "$@"; do if [[ -v $k ]]; then printf "%s=%s\0" "$k" "${!k}"; fi; done' _"""


def _minimal_env() -> dict[str, str]:
    # The directory of the bash under test leads PATH, so the child `bash -c` is the same bash.
    env = {"PATH": os.pathsep.join([str(Path(BASH).parent), os.environ["PATH"]])}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


@unittest.skipUnless(SCRIPTS.is_dir() and BASH, "scripts/ or bash is not available here")
class LoadRepoEnvTests(unittest.TestCase):
    def load(
        self, env_text: str | bytes, *, prelude: str = "", watch: tuple[str, ...] = ()
    ) -> tuple[dict[str, str], Path, str]:
        """The watched variables a child sees after load_repo_env, the temp dir (for markers), stderr."""
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        temp = Path(tempdir.name)
        env_file = temp / "scratch.env"
        data = env_text if isinstance(env_text, bytes) else env_text.encode()
        env_file.write_bytes(data.replace(b"{MARKER}", _bash_path(temp / "marker").encode()))
        keys = sorted({*KEY_RE.findall(data.decode("utf-8", "replace")), *watch})
        script = (
            f'source "{_bash_path(SCRIPTS / "load_env.sh")}"; {prelude} '
            f'load_repo_env "{_bash_path(env_file)}"; {PRINT_KEYS} {" ".join(keys)}'
        )
        # Bytes, decoded by hand: text mode would turn a stray carriage return into a newline.
        result = subprocess.run([BASH, "-c", script], capture_output=True, timeout=60, env=_minimal_env())
        stderr = result.stderr.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, stderr)
        pairs = [chunk.split("=", 1) for chunk in result.stdout.decode("utf-8").split("\0") if "=" in chunk]
        return {key: value for key, value in pairs}, temp, stderr

    def test_a_command_substitution_in_a_value_is_not_run(self) -> None:
        exported, temp, _ = self.load("SAFE=1\nX=$(touch {MARKER})\nY=`touch {MARKER}`\nZ=a;touch {MARKER}\n")

        self.assertFalse((temp / "marker").exists(), "the .env line was executed")
        self.assertEqual(exported["SAFE"], "1")
        self.assertTrue(exported["X"].startswith("$(touch "), exported["X"])
        self.assertTrue(exported["Y"].startswith("`touch "), exported["Y"])
        self.assertTrue(exported["Z"].startswith("a;touch "), exported["Z"])

    def test_values_keep_equals_signs_and_shell_characters(self) -> None:
        exported, _, _ = self.load("URL=postgres://u:p@h/db?a=b=c\nAMP=a&b|c(d)\nPATH_LIKE=$HOME/x\n")

        self.assertEqual(exported["URL"], "postgres://u:p@h/db?a=b=c")
        self.assertEqual(exported["AMP"], "a&b|c(d)")
        self.assertEqual(exported["PATH_LIKE"], "$HOME/x")

    def test_quoted_values_are_literal_and_unexpanded(self) -> None:
        exported, _, _ = self.load(
            'A="hello world"\nB=\'single $HOME\'\nC="it\'s"\nD="a # b"\nF=\'"nested"\'\nG="$HOME/x"\nH="a\\tb"\n'
        )

        self.assertEqual(exported["A"], "hello world")
        self.assertEqual(exported["B"], "single $HOME")
        self.assertEqual(exported["C"], "it's")
        self.assertEqual(exported["D"], "a # b")
        self.assertEqual(exported["F"], '"nested"')
        self.assertEqual(exported["G"], "$HOME/x")
        # \t is an escape in double quotes, as in docker compose.
        self.assertEqual(exported["H"], "a\tb")

    def test_double_quotes_handle_escaped_quote_backslash_and_newline(self) -> None:
        exported, _, stderr = self.load('ESC="a\\"b"\nBS="a\\\\b"\nNL="a\\nb"\nEND="x\\\\"\n')

        self.assertEqual(exported["ESC"], 'a"b')
        self.assertEqual(exported["BS"], "a\\b")
        self.assertEqual(exported["NL"], "a\nb")
        self.assertEqual(exported["END"], "x\\")
        self.assertEqual(stderr, "")

    def test_text_after_a_closing_quote_is_an_error_line(self) -> None:
        exported, _, stderr = self.load("GOOD=1\nAFTER=\"x\"y\nSINGLE='x'y\nOK=\"x\" # note\nOK2='x'   # note\n")

        self.assertNotIn("AFTER", exported)
        self.assertNotIn("SINGLE", exported)
        self.assertEqual((exported["OK"], exported["OK2"]), ("x", "x"))
        self.assertIn(":2:", stderr)
        self.assertIn(":3:", stderr)

    def test_an_unterminated_quote_warns_and_is_skipped(self) -> None:
        exported, _, stderr = self.load('GOOD=1\nPEM="-----BEGIN\nabc\n-----END"\nS=\'open\n')

        self.assertEqual(exported["GOOD"], "1")
        self.assertNotIn("PEM", exported)
        self.assertNotIn("S", exported)
        self.assertIn(":2:", stderr)
        self.assertIn(":5:", stderr)
        self.assertNotIn("BEGIN", stderr)

    def test_double_quote_escapes_match_compose_go(self) -> None:
        # compose-go dotenv/parser.go: escapeSeqRegex = `\\(?:[abcfnrtv$"\\]|0\d{0,3})`, applied by expandEscapes.
        # \$ is a literal $ (compose turns it into $$), \0 plus three octal digits is that character, and a backslash
        # before any other character stays as written.
        exported, _, stderr = self.load(
            'T="a\\tb"\nA="a\\ab"\nB="a\\bb"\nF="a\\fb"\nV="a\\vb"\nD="a\\$b"\nO="a\\0101b"\nQ="a\\qb"\n'
            'C="a\\cb"\nSHORT="a\\012b"\nBAD="a\\0789b"\nBIG="a\\0400b"\nSQ="a\\\'b"\n'
        )

        self.assertEqual(exported["T"], "a\tb")
        self.assertEqual(exported["A"], "a\x07b")
        self.assertEqual(exported["B"], "a\x08b")
        self.assertEqual(exported["F"], "a\x0cb")
        self.assertEqual(exported["V"], "a\x0bb")
        self.assertEqual(exported["D"], "a$b")
        self.assertEqual(exported["O"], "aAb")
        self.assertEqual(exported["Q"], "a\\qb")
        self.assertEqual(exported["C"], "a\\cb")
        self.assertEqual(exported["SHORT"], "a\\012b")  # \0 needs three digits after it
        self.assertEqual(exported["BAD"], "a\\0789b")  # 8 and 9 are not octal digits
        self.assertEqual(exported["BIG"], "a\\0400b")  # above 255: Go's UnquoteChar refuses it
        self.assertEqual(exported["SQ"], "a\\'b")
        self.assertEqual(stderr, "")

    def test_an_escape_the_loader_cannot_hold_is_skipped_with_a_warning(self) -> None:
        # \0000 is a NUL and \0200 a byte above ASCII, which compose-go turns into a two-byte rune: bash cannot
        # hold the first, and the second would differ, so docker compose reads these from .env itself.
        exported, _, stderr = self.load('NUL="a\\0000b"\nHIGH="a\\0200b"\nOK=1\n')

        self.assertNotIn("NUL", exported)
        self.assertNotIn("HIGH", exported)
        self.assertEqual(exported["OK"], "1")
        self.assertIn(":1:", stderr)
        self.assertIn(":2:", stderr)
        self.assertIn("escape", stderr)

    def test_single_quotes_keep_backslashes_but_an_escaped_quote_is_a_quote(self) -> None:
        # compose-go applies the escaped-quote rule to both quote characters, and expands escapes only in double
        # quotes, so a backslash pair stays a pair in single quotes.
        exported, _, stderr = self.load("A='it\\'s'\nB='x\\\\y'\nC='a\\tb'\nD='x\\\\'\n")

        self.assertEqual(exported["A"], "it's")
        self.assertEqual(exported["B"], "x\\\\y")
        self.assertEqual(exported["C"], "a\\tb")
        self.assertEqual(exported["D"], "x\\\\")
        self.assertEqual(stderr, "")

    def test_the_lines_of_an_unterminated_quote_are_skipped_until_it_closes(self) -> None:
        # A PEM's base64 padding is KEY=VALUE-shaped: it must not be exported as a key.
        pem = 'PEM="-----BEGIN KEY-----\nMIIBabc\nAbCdEfGh12==\n-----END KEY-----"\nAFTER=1\n'
        exported, _, stderr = self.load("BEFORE=0\n" + pem)

        self.assertEqual((exported["BEFORE"], exported["AFTER"]), ("0", "1"))
        self.assertNotIn("PEM", exported)
        self.assertNotIn("AbCdEfGh12", exported)
        self.assertEqual(stderr.count("load_repo_env"), 1, stderr)
        self.assertIn(":2:", stderr)
        self.assertNotIn("MIIB", stderr)

    def test_a_quote_that_never_closes_skips_the_rest_of_the_file_with_one_warning(self) -> None:
        exported, _, stderr = self.load("BEFORE=0\nPEM='-----BEGIN\nAbCdEfGh12==\nTAIL=1\n")

        self.assertEqual(exported["BEFORE"], "0")
        self.assertNotIn("AbCdEfGh12", exported)
        self.assertNotIn("TAIL", exported)
        self.assertEqual(stderr.count("load_repo_env"), 1, stderr)
        self.assertIn(":2:", stderr)

    def test_comments_blanks_export_prefix_and_crlf(self) -> None:
        exported, _, _ = self.load(
            "# a comment\n\n   \n  INDENT=1\nexport EXPORTED=2\nLOG=INFO  # trailing note\nWIN=3\r\nEMPTY=\n"
            'EMPTY_WITH_NOTE=   # set me\nHASH=#kept\nQ="q"\r\nnot a pair\n9BAD=x\n'
        )

        self.assertEqual(exported["INDENT"], "1")
        self.assertEqual(exported["EXPORTED"], "2")
        self.assertEqual(exported["LOG"], "INFO")
        self.assertEqual(exported["WIN"], "3")
        self.assertEqual(exported["EMPTY"], "")
        self.assertEqual(exported["EMPTY_WITH_NOTE"], "")
        self.assertEqual(exported["HASH"], "#kept")
        self.assertEqual(exported["Q"], "q")
        self.assertNotIn("9BAD", exported)

    def test_a_utf8_bom_before_the_first_key_is_stripped(self) -> None:
        exported, _, _ = self.load(b"\xef\xbb\xbfBOMKEY=1\nNEXT=2\n")

        self.assertEqual((exported["BOMKEY"], exported["NEXT"]), ("1", "2"))

    def test_every_unparseable_line_warns_with_its_number_and_never_its_text(self) -> None:
        exported, _, stderr = self.load("A=1\n# note\n\nKEY value-with-secret-xyz\n9BAD=secret-abc\nB=2\n")

        self.assertEqual((exported["A"], exported["B"]), ("1", "2"))
        self.assertIn(":4:", stderr)
        self.assertIn(":5:", stderr)
        self.assertNotIn(":2:", stderr)
        self.assertNotIn(":3:", stderr)
        self.assertNotIn("secret", stderr)

    def test_a_last_line_without_a_newline_is_read(self) -> None:
        exported, _, _ = self.load("FIRST=1\nLAST=2")

        self.assertEqual(exported["LAST"], "2")

    def test_a_missing_file_is_not_an_error(self) -> None:
        result = subprocess.run(
            [BASH, "-c", f'source "{_bash_path(SCRIPTS / "load_env.sh")}"; load_repo_env /no/such/file; echo done'],
            capture_output=True,
            text=True,
            timeout=60,
            env=_minimal_env(),
        )

        self.assertEqual((result.returncode, result.stdout.strip()), (0, "done"))

    def test_bash_env_cannot_run_a_command_in_a_child_shell(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        temp = Path(tempdir.name)
        marker = _bash_path(temp / "marker")
        env_file = temp / "scratch.env"
        env_file.write_text(f"BASH_ENV=$(touch {marker})\nENV=$(touch {marker})\nSAFE=1\n", encoding="utf-8")
        script = (
            f'source "{_bash_path(SCRIPTS / "load_env.sh")}"; load_repo_env "{_bash_path(env_file)}"; '
            "bash -c true; bash -lc true; printf '%s' \"${SAFE-unset}\""
        )
        result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=60, env=_minimal_env())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((temp / "marker").exists(), "BASH_ENV from .env ran in a child shell")
        self.assertEqual(result.stdout, "1")
        self.assertIn("BASH_ENV", result.stderr)
        self.assertNotIn("touch", result.stderr)

    def test_keys_that_change_the_shell_or_the_loader_are_refused(self) -> None:
        denied = [
            "BASH_ENV", "ENV", "BASH_FUNC_evil", "SHELLOPTS", "BASHOPTS", "IFS", "PATH", "PS4", "PROMPT_COMMAND",
            "CDPATH", "GLOBIGNORE", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "SMOKE_PROJECT",
            "SMOKE_COMPOSE_PROJECT", "ROOT_DIR", "COMPOSE",
        ]  # fmt: skip
        env_text = "".join(f"{key}=poisoned-{index}\n" for index, key in enumerate(denied)) + "FINE=1\n"
        exported, _, stderr = self.load(env_text, prelude="SMOKE_PROJECT=mine; ROOT_DIR=/repo; ")

        self.assertEqual(exported["FINE"], "1")
        for index, key in enumerate(denied):
            with self.subTest(key=key):
                self.assertNotEqual(exported.get(key), f"poisoned-{index}")
                self.assertIn(key, stderr)
        self.assertNotIn("poisoned", stderr)

    def test_keys_that_make_a_child_python_perl_node_or_browser_run_code_are_refused(self) -> None:
        # PYTHONWARNINGS imports any module named in it at startup, and BROWSER is what webbrowser.open() runs;
        # HOME decides which profile `bash -lc` reads. Each key is loaded alone: FUNCNEST=1 breaks the loader itself.
        denied = [
            "PYTHONWARNINGS", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONINSPECT", "PYTHONUSERBASE",
            "BROWSER", "PERL5OPT", "PERL5LIB", "NODE_OPTIONS", "HOME",
            "POSIXLY_CORRECT", "BASH_COMPAT", "EXECIGNORE", "FUNCNEST", "TMOUT", "LC_ALL", "LC_CTYPE",
        ]  # fmt: skip
        for key in denied:
            with self.subTest(key=key):
                exported, _, stderr = self.load(f"{key}=poisoned\nFINE=1\n")

                self.assertNotEqual(exported.get(key), "poisoned")
                self.assertEqual(exported["FINE"], "1")
                self.assertIn(f": {key} is not loaded from .env (", stderr)
                self.assertNotIn("poisoned", stderr)

    def test_on_windows_shells_the_denylist_ignores_the_case_of_a_key(self) -> None:
        # Windows treats environment names case-insensitively, so `Path=` would reach a child as PATH.
        denied = ["Path", "ComSpec", "Ld_Preload", "Bash_Env", "Pythonpath"]
        env_text = "".join(f"{key}=poisoned\n" for key in denied) + "FINE=1\n"
        for ostype in ("msys", "cygwin"):
            with self.subTest(ostype=ostype):
                exported, _, stderr = self.load(env_text, prelude=f"OSTYPE={ostype}; ")

                self.assertEqual(exported["FINE"], "1")
                for key in denied:
                    self.assertNotEqual(exported.get(key), "poisoned", key)
                    self.assertIn(f": {key} is not loaded from .env (", stderr)

    def test_elsewhere_a_key_differing_only_in_case_is_an_ordinary_key(self) -> None:
        exported, _, stderr = self.load("Ld_Preload=kept\nFINE=1\n", prelude="OSTYPE=linux-gnu; ")

        self.assertEqual((exported["Ld_Preload"], exported["FINE"]), ("kept", "1"))
        self.assertEqual(stderr, "")

    def test_only_the_smoke_scripts_own_names_are_refused(self) -> None:
        # doctor.sh reads SMOKE_URL, SMOKE_PROVIDER and SMOKE_GOAL from .env.
        ours = [
            "SMOKE_PROJECT", "SMOKE_COMPOSE_PROJECT", "SMOKE_SESSION_PREFIX", "SMOKE_MANAGED_LABEL",
            "SMOKE_MODE_LABEL", "SMOKE_SESSION_ID_LABEL", "SMOKE_COMPOSE_DIR_LABEL", "SMOKE_COMPOSE_PROJECT_LABEL",
        ]  # fmt: skip
        env_text = "SMOKE_URL=https://example.test\nSMOKE_PROVIDER=claude\nSMOKE_GOAL=look\n" + "".join(
            f"{key}=poisoned\n" for key in ours
        )
        exported, _, stderr = self.load(env_text)

        self.assertEqual(
            (exported["SMOKE_URL"], exported["SMOKE_PROVIDER"], exported["SMOKE_GOAL"]),
            ("https://example.test", "claude", "look"),
        )
        for key in ours:
            self.assertNotEqual(exported.get(key), "poisoned", key)
            self.assertIn(f": {key} is not loaded from .env (", stderr)
        self.assertNotIn("SMOKE_URL", stderr)

    def test_a_variable_the_script_set_before_loading_is_not_overwritten(self) -> None:
        exported, _, stderr = self.load(
            "MY_INTERNAL=from-env\nEXPORTED_ONE=from-env\n",
            prelude="MY_INTERNAL=mine; export EXPORTED_ONE=old; ",
            watch=("MY_INTERNAL",),
        )

        self.assertNotIn("MY_INTERNAL", exported)  # set but not exported, and .env does not export it
        self.assertEqual(exported["EXPORTED_ONE"], "from-env")  # an exported variable stays overridable
        self.assertIn("MY_INTERNAL", stderr)

    def test_the_parsers_own_names_are_ordinary_keys(self) -> None:
        exported, _, stderr = self.load("pair_re=.*\nline=L\nkey=K\nvalue=V\nenv_file=E\nOTHER=1\n")

        self.assertEqual(stderr, "")
        self.assertEqual(
            (exported["pair_re"], exported["line"], exported["key"], exported["value"], exported["env_file"]),
            (".*", "L", "K", "V", "E"),
        )
        self.assertEqual(exported["OTHER"], "1")


# Syntax that needs bash 4 or later. macOS ships bash 3.2, and the repo's scripts support it (doctor.sh keeps
# `${a[@]+...}` for an empty array under `set -u`): in 3.2 one of these is a parse error that aborts `source`.
BASH4_ONLY = {
    "[[ -v (bash 4.2)": re.compile(r"\[\[\s+(?:!\s+)?-v\s"),
    "mapfile": re.compile(r"\bmapfile\b"),
    "readarray": re.compile(r"\breadarray\b"),
    "declare -A / -n": re.compile(r"\b(?:declare|typeset|local)\s+-[a-zA-Z]*[An]"),
    "${x,,} / ${x^^}": re.compile(r"\$\{[A-Za-z_][A-Za-z_0-9]*(?:,,?|\^\^?)[^}]*\}"),
    "|&": re.compile(r"\|&"),
    "&>>": re.compile(r"&>>"),
    "coproc": re.compile(r"\bcoproc\b"),
    "wait -n": re.compile(r"\bwait\s+-[a-zA-Z]*n"),
    "${x@Q}": re.compile(r"\$\{[^}]*@[QEPAa]\}"),
}


@unittest.skipUnless(SCRIPTS.is_dir(), "scripts/ is not available here")
class HostPythonIsIsolatedTests(unittest.TestCase):
    def test_every_host_python_a_loading_script_starts_ignores_the_environment(self) -> None:
        # A .env value reaches these children, and python reads PYTHONWARNINGS, PYTHONSTARTUP and the like at startup
        # unless it runs with -I. (Containers' `docker exec ... python3 -I` is already isolated.)
        loaders = [s for s in sorted(SCRIPTS.glob("*.sh")) if "scripts/load_env.sh" in s.read_text(encoding="utf-8")]
        self.assertGreaterEqual(len(loaders), 6)
        for script in loaders:
            with self.subTest(script=script.name):
                bare = [
                    f"{number}: {line.strip()}"
                    for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), start=1)
                    if re.search(r"(?<![\w./-])python3?(?:\.\d+)?\s+(?!-\w*I)", line)
                    and not line.lstrip().startswith("#")
                ]
                self.assertEqual(bare, [])


@unittest.skipUnless(SCRIPTS.is_dir(), "scripts/ is not available here")
class ScriptsStayBash32CompatibleTests(unittest.TestCase):
    def test_the_detector_sees_each_bash4_construct(self) -> None:
        samples = {
            "[[ -v (bash 4.2)": 'if [[ -v "$1" ]]; then',
            "mapfile": "mapfile -t lines < file",
            "readarray": "readarray -t lines < file",
            "declare -A / -n": "declare -A table",
            "${x,,} / ${x^^}": 'echo "${name,,} ${name^^}"',
            "|&": "cmd |& tee log",
            "&>>": "cmd &>> log",
            "coproc": "coproc cat",
            "wait -n": "wait -n",
            "${x@Q}": 'echo "${name@Q}"',
        }
        self.assertEqual(set(samples), set(BASH4_ONLY))
        for label, sample in samples.items():
            with self.subTest(label=label):
                self.assertRegex(sample, BASH4_ONLY[label])

    def test_no_script_uses_bash4_only_syntax(self) -> None:
        for script in sorted(SCRIPTS.glob("*.sh")):
            found = [
                f"{number}: {label}"
                for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), start=1)
                if not line.lstrip().startswith("#")
                for label, pattern in BASH4_ONLY.items()
                if pattern.search(line)
            ]
            with self.subTest(script=script.name):
                self.assertEqual(found, [], f"{script.name} uses syntax that bash 3.2 rejects")


if __name__ == "__main__":
    unittest.main()
