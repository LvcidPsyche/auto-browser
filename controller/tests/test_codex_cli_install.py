"""The image's codex is the glibc build from Codex's own GitHub release, checked against pinned sha256s.

The npm package @openai/codex ships the musl build of the binary. Its sandbox helper opens / with O_PATH
through Rust std, which drops the flag on musl (musl's O_ACCMODE includes O_PATH), so under the controller's Landlock
launcher the open reached the kernel as O_RDONLY and was refused: under read-only and workspace-write no command of
Codex's model ran. The release's glibc build keeps O_PATH; the agent-cli-image workflow checks what its sandbox does
under the launcher ("Codex's own sandbox" in docs/deployment.md). A repo-layout check: the controller image ships only
app/ and tests/, so it skips there.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from tests._bash import BASH, NO_BASH_REASON

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "controller" / "Dockerfile"
NOT_IN_THIS_TREE = "controller/Dockerfile is not in this tree (the image ships app/ and tests/)"
# Each architecture dpkg names: the release's name for it, then the ARGs of its tarball's and its binary's sums.
BUILDS = {
    "amd64": ("x86_64", "CODEX_CLI_TARBALL_SHA256_AMD64", "CODEX_CLI_BINARY_SHA256_AMD64"),
    "arm64": ("aarch64", "CODEX_CLI_TARBALL_SHA256_ARM64", "CODEX_CLI_BINARY_SHA256_ARM64"),
}
SUM_ARGS = [name for _, *names in BUILDS.values() for name in names]
RELEASE_ASSET = re.compile(
    r"https://github\.com/openai/codex/releases/download/rust-v\$\{?CODEX_CLI_VERSION\}?/"
    r"codex-\$\{?codex_arch\}?-unknown-linux-gnu\.tar\.gz"
)
ARCHITECTURE_CASE = re.compile(r'case "\$\(dpkg --print-architecture\)" in .*? esac')
# Download, check, extract, check, install, each step joined to the next by && alone: each check reads the file the
# step before it wrote, the second checks a file of the directory extracted to, and the install takes that file (the
# same paths, by backreference).
INSTALL_CHAIN = re.compile(
    r'curl -fsSL -o (?P<tarball>\S+)\s+"' + RELEASE_ASSET.pattern + '"'
    r'\s+&&\s+echo "\$\{codex_tar_sum\}  (?P=tarball)" \| sha256sum -c -'
    r"\s+&&\s+tar -xzf (?P=tarball) -C (?P<directory>\S+)"
    r'\s+&&\s+echo "\$\{codex_bin_sum\}  (?P<binary>(?P=directory)/[^"/]+)" \| sha256sum -c -'
    r'\s+&&\s+install -m 0755 -o root -g root "(?P=binary)" /usr/local/bin/codex\s+&&'
)
# The requirements file: printf'd to a staged path, then installed from that path, root-owned 0644.
REQUIREMENTS = re.compile(
    r"&& (?P<printf>printf '%s\\n' '[^']*'\s+'mcp_servers = \{\}') > (?P<staged>\S+)"
    r"\s+&&\s+install -D -m 0644 -o root -g root (?P=staged) /etc/codex/requirements\.toml\s+&&"
)


def _dockerfile() -> str:
    """The Dockerfile's instructions, without its comments and with line continuations joined: one RUN, one line."""
    instructions = re.sub(r"^\s*#.*\n", "", DOCKERFILE.read_text(encoding="utf-8"), flags=re.M)
    return re.sub(r"\\\n\s*", " ", instructions)


def _args() -> dict[str, str]:
    return dict(re.findall(r"^ARG (\w+)=(\S*)$", DOCKERFILE.read_text(encoding="utf-8"), re.M))


@unittest.skipUnless(DOCKERFILE.is_file(), NOT_IN_THIS_TREE)
class CodexComesFromItsReleaseTests(unittest.TestCase):
    def test_npm_installs_the_claude_and_gemini_clis_and_no_codex(self) -> None:
        text = _dockerfile()
        # Nowhere, in whatever form: npm i, npm add, --global, npx.
        self.assertNotIn("@openai/codex", text)
        installs = re.findall(r"npm (?:install|i|add) (?:-g|--global) ([^&;]*)", text)
        self.assertEqual(len(installs), 1, installs)
        self.assertIn("@anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}", installs[0])
        self.assertIn("@google/gemini-cli@${GEMINI_CLI_VERSION}", installs[0])

    def test_the_version_and_each_sum_are_set_once(self) -> None:
        # A second ARG would hide from these tests the value an earlier RUN takes.
        text = DOCKERFILE.read_text(encoding="utf-8")
        for name in ["CODEX_CLI_VERSION", *SUM_ARGS]:
            with self.subTest(arg=name):
                self.assertEqual(len(re.findall(rf"^\s*ARG\s+{name}\b", text, re.M)), 1)

    def test_the_release_tarball_is_checked_extracted_and_its_checked_binary_installed(self) -> None:
        self.assertRegex(_dockerfile(), INSTALL_CHAIN)

    def test_the_pin_is_older_than_the_release_whose_helper_defaults_to_bubblewrap(self) -> None:
        # From 0.115.0 Codex's helper defaults to bubblewrap, whose mounts Landlock refuses (inferred, not measured):
        # such a pin needs another design ("Codex's own sandbox" in docs/deployment.md), not only new sums.
        pin = _args()["CODEX_CLI_VERSION"]
        self.assertRegex(pin, r"^\d+\.\d+\.\d+$")  # "0.115" would compare below (0, 115, 0)
        self.assertLess(tuple(int(part) for part in pin.split(".")), (0, 115, 0))

    def test_each_pinned_sha256_is_64_hex_characters(self) -> None:
        args = _args()
        for name in SUM_ARGS:
            with self.subTest(arg=name):
                self.assertRegex(args.get(name, ""), r"^[0-9a-f]{64}$")


@unittest.skipUnless(DOCKERFILE.is_file(), NOT_IN_THIS_TREE)
@unittest.skipIf(BASH is None, NO_BASH_REASON)
class NoMcpServerLoadsTests(unittest.TestCase):
    """/etc/codex/requirements.toml holds `mcp_servers = {}`: a `-c mcp_servers={}` clears no server a
    CODEX_HOME config.toml names (Codex merges -c tables key by key), and the file lies outside every grant of the CLI
    sandbox. The agent-cli-image workflow checks that Codex reads it so."""

    def test_the_cli_branch_alone_writes_the_requirements_root_owned_0644(self) -> None:
        text = _dockerfile()
        branch = re.search(r'if \[ "\$INSTALL_AGENT_CLIS" = "true" \]; then (.*?) elif ', text)
        self.assertIsNotNone(branch, "no INSTALL_AGENT_CLIS=true branch")
        written = REQUIREMENTS.search(branch.group(1))
        self.assertIsNotNone(written, "the CLI branch does not install /etc/codex/requirements.toml, root:root 0644")
        self.assertNotIn("/etc/codex", text.replace(branch.group(1), ""))
        with tempfile.TemporaryDirectory() as scratch:
            staged = Path(scratch) / "requirements.toml"
            subprocess.run(
                [BASH, "--noprofile", "--norc", "-c", f'{written.group("printf")} > "$0"', staged.as_posix()],
                check=True,
                timeout=30,
            )
            lines = staged.read_text(encoding="utf-8").splitlines()
            self.assertEqual(tomllib.loads(staged.read_text(encoding="utf-8")), {"mcp_servers": {}})
        self.assertTrue(lines[0].startswith("# "), lines)


@unittest.skipUnless(DOCKERFILE.is_file(), NOT_IN_THIS_TREE)
@unittest.skipIf(BASH is None, NO_BASH_REASON)
class TheArchitectureChoosesTheTarballTests(unittest.TestCase):
    """The Dockerfile's own case statement, run with dpkg answering for the architecture."""

    def _choose(self, architecture: str) -> subprocess.CompletedProcess[str]:
        case = ARCHITECTURE_CASE.search(_dockerfile())
        self.assertIsNotNone(case, "no case on dpkg --print-architecture")
        script = f'dpkg() {{ echo "$ARCH"; }}; {case.group(0)}; echo "$codex_arch $codex_tar_sum $codex_bin_sum"'
        return subprocess.run(
            [BASH, "--noprofile", "--norc", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={"ARCH": architecture, **{name: name.lower() for name in SUM_ARGS}},
        )

    def test_amd64_and_arm64_take_their_own_build_and_sums(self) -> None:
        for architecture, (machine, tarball, binary) in BUILDS.items():
            with self.subTest(architecture=architecture):
                result = self._choose(architecture)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.split(), [machine, tarball.lower(), binary.lower()])

    def test_any_other_architecture_stops_the_build_and_says_why(self) -> None:
        result = self._choose("riscv64")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("riscv64", result.stderr)
