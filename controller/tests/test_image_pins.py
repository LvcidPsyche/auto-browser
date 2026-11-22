"""Every image this repo builds starts from a base pinned by digest, and Dependabot proposes each base's next digest.

A FROM by tag alone builds on whatever the tag names on the day of the build; by digest it builds on one image. The
pin would freeze the base's security patches without Dependabot's docker entry, which proposes the tag's newer digest
(with the cooldown the other entries have), so the two land together. The root (Glama) image installs under the
controller's constraints.txt. Each build context also has a .dockerignore that keeps the checkout's secrets, .venv*,
node_modules and __pycache__ out of what the build is sent, and nothing its Dockerfile copies. A repo-layout check: the
controller image ships only app/ and tests/, so it skips there.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
# The directory of each Dockerfile, which is also its build context.
CONTEXTS = ("", "controller", "browser-node", "reverse-ssh", "smoke/bastion")
PINNED = re.compile(r"^[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}$")
# Paths no build may be sent, wherever a checkout holds them: history, .venv*, node_modules, __pycache__, the
# runtime data (data/ holds the API token and the keys) and .env files.
RECURSIVE = ("**/.venv*", "**/node_modules", "**/__pycache__", "**/.env*")
PRIVATE = (
    ".git/config",
    ".venv/bin/python",
    ".venv-codex/bin/python",
    "app/.venv/bin/python",
    "node_modules/x/index.js",
    "app/node_modules/x/index.js",
    "__pycache__/m.pyc",
    "app/__pycache__/m.pyc",
    "data/api-token",
    ".env",
    ".env.local",
    "app/.env",
)


def _dependabot_docker_directories() -> dict[str, dict]:
    config = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    return {
        directory: entry
        for entry in config["updates"]
        if entry["package-ecosystem"] == "docker"
        for directory in entry.get("directories") or [entry["directory"]]
    }


def _ignore_lines(context: str) -> list[str]:
    text = (REPO / context / ".dockerignore").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _pattern(line: str) -> re.Pattern[str]:
    """A .dockerignore pattern as Docker reads it: `**` any number of directories, `*` and `?` within one name. (No
    `[...]` class is used in these files, so none is translated.)"""
    pattern, regex, i = line.lstrip("/"), "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            regex, i = regex + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            regex, i = regex + ".*", i + 2
        else:
            regex += {"*": "[^/]*", "?": "[^/]"}.get(pattern[i], re.escape(pattern[i]))
            i += 1
    return re.compile(regex + r"\Z")


def _ignored(path: str, patterns: list[re.Pattern[str]]) -> bool:
    """Whether Docker leaves `path` out of the context: a pattern matches it or a directory it lies in."""
    parts = path.strip("/").split("/")
    return any(pattern.match("/".join(parts[:end])) for pattern in patterns for end in range(1, len(parts) + 1))


def _copy_sources(context: str) -> list[str]:
    """What each COPY of the context's Dockerfile copies from the context."""
    text = (REPO / context / "Dockerfile").read_text(encoding="utf-8")
    sources: list[str] = []
    for line in re.findall(r"^COPY\s+(.+)$", text, re.M):
        words = [word for word in line.split() if not word.startswith("--")]
        sources += [word.removeprefix("./") for word in words[:-1]]
    return sources


@unittest.skipUnless((REPO / "controller" / "Dockerfile").is_file(), "the repo's Dockerfiles are not in this tree")
class ImageBuildInputsTests(unittest.TestCase):
    def test_every_from_names_its_tag_and_a_digest(self) -> None:
        for context in CONTEXTS:
            text = (REPO / context / "Dockerfile").read_text(encoding="utf-8")
            bases = re.findall(r"^FROM\s+(\S+)", text, re.M)
            with self.subTest(dockerfile=f"{context or '.'}/Dockerfile"):
                self.assertTrue(bases)
                for base in bases:
                    self.assertRegex(base, PINNED)

    def test_dependabot_proposes_each_base_s_next_digest_with_the_cooldown(self) -> None:
        entries = _dependabot_docker_directories()
        for context in CONTEXTS:
            with self.subTest(directory=f"/{context}"):
                entry = entries.get(f"/{context}")
                self.assertIsNotNone(entry, f"no docker entry for /{context} in .github/dependabot.yml")
                self.assertGreaterEqual(entry.get("cooldown", {}).get("default-days", 0), 3)
                # A new digest of the pinned tag, not a new tag: python:3.11-slim to 3.14-slim is a decision of its
                # own, which a weekly pull request is not.
                ignored = [
                    set(rule.get("update-types", []))
                    for rule in entry.get("ignore", [])
                    if rule.get("dependency-name") == "*"
                ]
                self.assertIn({"version-update:semver-major", "version-update:semver-minor"}, ignored)

    def test_the_root_image_installs_its_requirements_under_the_constraints(self) -> None:
        # The root (Glama) image installed requirements.txt alone, so each build resolved the tree afresh and could
        # take a release under 72 hours old; the controller image installs under controller/constraints.txt.
        text = (REPO / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^COPY controller/requirements\.txt controller/constraints\.txt \./$")
        instructions = re.sub(r"\\\n\s*", " ", re.sub(r"(?m)^\s*#.*\n", "", text))
        # Every pip install, in any RUN, chained or not, up to the next command of its line.
        installs = re.findall(r"\bpip3? install\b[^&;|\n]*", instructions)
        self.assertTrue(installs)
        for install in installs:
            with self.subTest(install=install):
                self.assertRegex(install, r"\s-c constraints\.txt(\s|$)")
        self.assertRegex(installs[0], r"-r requirements\.txt -c constraints\.txt")

    def test_each_build_context_keeps_the_checkout_s_secrets_and_caches_out(self) -> None:
        for context in CONTEXTS:
            with self.subTest(context=context or "."):
                self.assertTrue((REPO / context / ".dockerignore").is_file(), f"no .dockerignore in /{context}")
                lines = _ignore_lines(context)
                # An exception (`!data`) would let a path the lines above keep out back in.
                self.assertEqual([line for line in lines if line.startswith("!")], [])
                # The classes a checkout holds at any depth are kept out at any depth.
                self.assertLessEqual(set(RECURSIVE), set(lines))
                patterns = [_pattern(line) for line in lines]
                self.assertEqual([path for path in PRIVATE if not _ignored(path, patterns)], [])

    def test_no_build_context_keeps_out_what_its_dockerfile_copies(self) -> None:
        # Guards the check against a Dockerfile layout it no longer reads: the controller image copies both.
        self.assertLessEqual({"app", "tests"}, set(_copy_sources("controller")))
        for context in CONTEXTS:
            patterns = [_pattern(line) for line in _ignore_lines(context)]
            with self.subTest(context=context or "."):
                self.assertEqual([source for source in _copy_sources(context) if _ignored(source, patterns)], [])
