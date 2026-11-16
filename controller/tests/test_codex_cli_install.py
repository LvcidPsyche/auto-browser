"""The image's codex is the glibc build from Codex's own GitHub release, checked against pinned sha256s.

The npm package @openai/codex ships the musl build of the binary. Its sandbox helper opens / with O_PATH
through Rust std, which drops the flag on musl (musl's O_ACCMODE includes O_PATH), so under the controller's Landlock
launcher the open reached the kernel as O_RDONLY and was refused: Codex could run none of its model's commands. The
release's glibc build keeps O_PATH (measured on GitHub's amd64 and arm64 runners, 2026-11-16); the agent-cli-image
workflow runs that check on every change to the install. A repo-layout check: the controller image ships only app/
and tests/, so it skips there.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

from tests._bash import BASH, NO_BASH_REASON

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "controller" / "Dockerfile"
HASH_ARGS = {
    "amd64": ("CODEX_CLI_TARBALL_SHA256_AMD64", "CODEX_CLI_BINARY_SHA256_AMD64"),
    "arm64": ("CODEX_CLI_TARBALL_SHA256_ARM64", "CODEX_CLI_BINARY_SHA256_ARM64"),
}
NOT_IN_THIS_TREE = "controller/Dockerfile is not in this tree (the image ships app/ and tests/)"
ARCHITECTURES = {"amd64": "x86_64", "arm64": "aarch64"}
RELEASE_ASSET = re.compile(
    r"https://github\.com/openai/codex/releases/download/rust-v\$\{?CODEX_CLI_VERSION\}?/"
    r"codex-\$\{?codex_arch\}?-unknown-linux-gnu\.tar\.gz"
)
ARCHITECTURE_CASE = re.compile(r'case "\$\(dpkg --print-architecture\)" in .*? esac')
SUM_CHECK = re.compile(r'echo "\$\{?(\w+)\}?  ([^"]+)" \| sha256sum -c -')


def _dockerfile() -> str:
    """The Dockerfile's instructions, without its comments and with line continuations joined: one RUN, one line."""
    instructions = re.sub(r"^\s*#.*\n", "", DOCKERFILE.read_text(encoding="utf-8"), flags=re.M)
    return re.sub(r"\\\n\s*", " ", instructions)


def _args() -> dict[str, str]:
    return dict(re.findall(r"^ARG (\w+)=(\S*)$", DOCKERFILE.read_text(encoding="utf-8"), re.M))


@unittest.skipUnless(DOCKERFILE.is_file(), NOT_IN_THIS_TREE)
class CodexComesFromItsReleaseTests(unittest.TestCase):
    def test_npm_installs_the_claude_and_gemini_clis_and_no_codex(self) -> None:
        installs = re.findall(r"npm install -g ([^&;]*)", _dockerfile())
        self.assertEqual(len(installs), 1, installs)
        self.assertNotIn("@openai/codex", installs[0])
        self.assertIn("@anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}", installs[0])
        self.assertIn("@google/gemini-cli@${GEMINI_CLI_VERSION}", installs[0])

    def test_codex_is_the_glibc_tarball_of_its_pinned_release(self) -> None:
        text = _dockerfile()
        self.assertIsNotNone(RELEASE_ASSET.search(text), "no curl of rust-v${CODEX_CLI_VERSION}'s glibc tarball")
        self.assertIn("curl -fsSL", text)

    def test_the_pin_is_older_than_the_release_whose_helper_defaults_to_bubblewrap(self) -> None:
        # From 0.115.0 Codex's helper defaults to bubblewrap, whose mounts Landlock refuses (inferred, not measured):
        # such a pin needs another design ("Codex's own sandbox" in docs/deployment.md), not only new sums.
        version = tuple(int(part) for part in _args()["CODEX_CLI_VERSION"].split("."))
        self.assertLess(version, (0, 115, 0))

    def test_each_pinned_sha256_is_64_hex_characters(self) -> None:
        args = _args()
        for name in [name for pair in HASH_ARGS.values() for name in pair]:
            with self.subTest(arg=name):
                self.assertRegex(args.get(name, ""), r"^[0-9a-f]{64}$")

    def test_the_tarball_then_the_binary_are_checked_before_the_binary_is_installed(self) -> None:
        text = _dockerfile()
        checks = SUM_CHECK.findall(text)
        self.assertEqual(
            [(variable, path.endswith(".tar.gz")) for variable, path in checks],
            [("codex_tar_sum", True), ("codex_bin_sum", False)],
            checks,
        )
        tarball_check = text.index("| sha256sum -c -")
        extract = text.index("tar -xzf", tarball_check)
        binary_check = text.index("| sha256sum -c -", extract)
        install = re.search(r"install -m 0755 -o root -g root \S+ /usr/local/bin/codex", text[binary_check:])
        self.assertIsNotNone(install, "the checked binary is not installed as /usr/local/bin/codex, root-owned, 0755")


@unittest.skipUnless(DOCKERFILE.is_file(), NOT_IN_THIS_TREE)
@unittest.skipIf(BASH is None, NO_BASH_REASON)
class TheArchitectureChoosesTheTarballTests(unittest.TestCase):
    """The Dockerfile's own case statement, run with dpkg answering for the architecture."""

    def _choose(self, architecture: str) -> subprocess.CompletedProcess[str]:
        case = ARCHITECTURE_CASE.search(_dockerfile())
        self.assertIsNotNone(case, "no case on dpkg --print-architecture")
        script = f'dpkg() {{ echo "$ARCH"; }}; {case.group(0)}; echo "$codex_arch $codex_tar_sum $codex_bin_sum"'
        sums = {name: name.lower() for pair in HASH_ARGS.values() for name in pair}
        return subprocess.run(
            [BASH, "--noprofile", "--norc", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={"ARCH": architecture, **sums},
        )

    def test_amd64_and_arm64_take_their_own_build_and_sums(self) -> None:
        for architecture, machine in ARCHITECTURES.items():
            with self.subTest(architecture=architecture):
                result = self._choose(architecture)
                self.assertEqual(result.returncode, 0, result.stderr)
                tarball, binary = HASH_ARGS[architecture]
                self.assertEqual(result.stdout.split(), [machine, tarball.lower(), binary.lower()])

    def test_any_other_architecture_stops_the_build_and_says_why(self) -> None:
        result = self._choose("riscv64")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("riscv64", result.stderr)
