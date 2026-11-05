"""The repo's scripts never put the API token on a command line.

bearer_curl (scripts/load_env.sh) and doctor.sh's api_curl passed
-H "Authorization: Bearer <token>" to curl, and smoke_reverse_ssh.sh built the
same header into the `sh -lc` string it hands `docker compose exec`. A command
line is readable by every local user (ps, /proc/<pid>/cmdline), so the token
leaked for as long as each curl ran. The header now reaches curl as a config
read from stdin (`curl --config -`): nothing in argv, nothing on disk.

The dynamic test runs the real bearer_curl in bash against a stand-in curl that
records its argv and stdin. The static test covers every other script, the
Makefile and the workflows. Repo-layout checks: skipped where scripts/ is absent
(the controller image) or bash is missing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
TOKEN = "tok3n-" + "Q" * 37


def _find_bash() -> str | None:
    """bash to run the scripts with. On Windows, Git for Windows' bash (the one the
    scripts are run with there), not System32's bash.exe, which is WSL's."""
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    if git:
        # git.exe lives in Git\cmd or Git\mingw64\bin depending on the PATH entry.
        for root in Path(git).parents:
            for candidate in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)
    return None


BASH = _find_bash()

# A token-bearing header on a command line: in a script, -H/--header with a
# Bearer value built from a variable, or such a header string assigned for later use.
ARGV_TOKEN = re.compile(r"""(-H|--header)\s+["']?Authorization:\s*Bearer\s+\$|["']Authorization:\s*Bearer\s+\$\{?\w""")
# A curl that reads the token's config from stdin without -q (or --disable) as its first argument: curl honours it
# only there, and otherwise a .curlrc found through CURL_HOME, XDG_CONFIG_HOME or HOME can add a proxy or a header.
CURLRC_READ = re.compile(r"""\bcurl\s+(?!-q\b|--disable\b)[^|;&\n]*--config\s+-(?=[\s"']|$)""")


def _bash_path(path: Path) -> str:
    """A path bash on this host understands (Git Bash on Windows wants /c/...)."""
    text = path.as_posix()
    if os.name == "nt" and re.match(r"^[A-Za-z]:/", text):
        return f"/{text[0].lower()}{text[2:]}"
    return text


@unittest.skipUnless(SCRIPTS.is_dir() and BASH, "scripts/ or bash is not available here")
class BearerCurlTests(unittest.TestCase):
    def run_bearer_curl(self, *, token: str | None, xtrace: bool = False) -> tuple[str, str, str]:
        with tempfile.TemporaryDirectory() as tempdir:
            temp = Path(tempdir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            argv_log, stdin_log = temp / "argv.log", temp / "stdin.log"
            curl = fake_bin / "curl"
            curl.write_text(
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "$@" > "{_bash_path(argv_log)}"\n'
                f'cat > "{_bash_path(stdin_log)}"\n'
                "echo ok\n",
                encoding="utf-8",
                newline="\n",
            )
            curl.chmod(0o755)
            env = {key: value for key, value in os.environ.items() if key != "API_BEARER_TOKEN"}
            if token is not None:
                env["API_BEARER_TOKEN"] = token
            script = (
                f'export PATH="{_bash_path(fake_bin)}:$PATH"; '
                f'ROOT_DIR="{_bash_path(temp)}"; '
                f'source "{_bash_path(SCRIPTS / "load_env.sh")}"; '
                + ("set -x; " if xtrace else "")
                + "bearer_curl -fsS http://127.0.0.1:8000/sessions"
            )
            result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = argv_log.read_text(encoding="utf-8")
            stdin = stdin_log.read_text(encoding="utf-8") if stdin_log.exists() else ""
            self.trace = result.stderr
            return result.stdout, argv, stdin

    def test_a_set_x_trace_never_shows_the_token(self) -> None:
        """bash -x traces builtins and function arguments: a debug run must not print the token."""
        _, argv, stdin = self.run_bearer_curl(token=TOKEN, xtrace=True)

        self.assertIn("+ ", self.trace)  # the trace is on
        self.assertIn("curl -q --config -", self.trace)
        self.assertNotIn(TOKEN, self.trace)
        self.assertNotIn(TOKEN, argv)
        self.assertIn(f'header = "Authorization: Bearer {TOKEN}"', stdin)

    def test_the_token_reaches_curl_on_stdin_never_in_argv(self) -> None:
        stdout, argv, stdin = self.run_bearer_curl(token=TOKEN)

        self.assertEqual(stdout.strip(), "ok")
        self.assertNotIn(TOKEN, argv)
        self.assertNotIn("Authorization", argv)
        self.assertIn("--config", argv.splitlines())
        self.assertEqual(argv.splitlines()[argv.splitlines().index("--config") + 1], "-")
        self.assertIn(f'header = "Authorization: Bearer {TOKEN}"', stdin)
        # The caller's own arguments still reach curl.
        self.assertIn("http://127.0.0.1:8000/sessions", argv.splitlines())

    def test_curl_is_told_to_skip_its_own_config_file(self) -> None:
        # -q must be curl's first argument: a .curlrc (found through CURL_HOME, XDG_CONFIG_HOME or HOME) could
        # otherwise add a proxy or another header to a request that carries the token.
        _, argv, _ = self.run_bearer_curl(token=TOKEN)

        self.assertEqual(argv.splitlines()[0], "-q")

    def test_without_a_token_no_authorization_is_sent(self) -> None:
        _, argv, stdin = self.run_bearer_curl(token=None)

        self.assertNotIn("Authorization", argv + stdin)

    def test_a_token_with_a_line_break_is_refused_loudly_and_never_sent(self) -> None:
        # No HTTP header can carry one, and in a curl config it would split the line.
        for broken in (TOKEN[:20] + "\n" + TOKEN[20:], TOKEN + "\r"):
            with self.subTest(broken=repr(broken[-3:])):
                _, argv, stdin = self.run_bearer_curl(token=broken)

                self.assertNotIn("Authorization", argv + stdin)
                self.assertIn("line break", self.trace)
                self.assertNotIn(TOKEN[:20], self.trace)


@unittest.skipUnless(SCRIPTS.is_dir(), "scripts/ is not shipped inside the controller image")
class NoTokenInArgvTests(unittest.TestCase):
    def test_no_script_makefile_or_workflow_puts_a_bearer_token_in_argv(self) -> None:
        sources = [*SCRIPTS.glob("*.sh"), REPO / "Makefile", *(REPO / ".github" / "workflows").glob("*.yml")]
        offenders = []
        for path in sources:
            if not path.is_file():
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                if ARGV_TOKEN.search(line):
                    offenders.append(f"{path.relative_to(REPO).as_posix()}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "a bearer token reaches a command line")

    def test_the_pattern_still_catches_the_old_forms(self) -> None:
        # Guards the regex: these are the three forms this change removed.
        for line in (
            '    curl -H "Authorization: Bearer ${token}" "$@"',
            '    args+=(-H "Authorization: Bearer ${token}")',
            """  BASTION_AUTH="-H 'Authorization: Bearer ${BASTION_TOKEN}'\"""",
        ):
            with self.subTest(line=line):
                self.assertRegex(line, ARGV_TOKEN)

    def test_every_curl_that_reads_the_token_from_stdin_skips_its_curlrc(self) -> None:
        # D3b-7: bearer_curl got -q; doctor.sh's api_curl and smoke_reverse_ssh.sh's bastion curl did not.
        offenders = []
        for path in sorted(SCRIPTS.glob("*.sh")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.lstrip().startswith("#") and CURLRC_READ.search(line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "a curl reads a .curlrc next to the token")

    def test_the_curlrc_pattern_catches_the_old_forms_and_passes_the_fixed_one(self) -> None:
        for line in (
            '  curl_auth_config | curl --config - ${args[@]+"${args[@]}"} "$@"',
            '  curl_auth_config | "${COMPOSE[@]}" exec -T test-bastion sh -lc "curl --config - $1"',
            "  curl_auth_config | curl -fsS --config - http://127.0.0.1:8000/",
        ):
            with self.subTest(line=line):
                self.assertRegex(line, CURLRC_READ)
        self.assertNotRegex('  curl_auth_config | curl -q --config - "$@"', CURLRC_READ)


if __name__ == "__main__":
    unittest.main()
