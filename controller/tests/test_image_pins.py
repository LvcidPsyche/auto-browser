"""Every image this repo builds starts from a base pinned by digest, and Dependabot proposes each base's next digest.

A FROM by tag alone builds on whatever the tag names on the day of the build; by digest it builds on one image. The
pin would freeze the base's security patches without Dependabot's docker entries, which propose the tag's newer
digest (after the 72-hour cooldown), so the two land together. Each build context also has a .dockerignore that keeps
the checkout's secrets, virtualenvs and caches out of what the build is sent. A repo-layout check: the controller image
ships only app/ and tests/, so it skips there.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
# The directory of each Dockerfile, which is also its build context.
CONTEXTS = ("", "controller", "browser-node", "reverse-ssh", "smoke/bastion")
PINNED = re.compile(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}")
KEPT_OUT = (".git", ".venv*", "node_modules", "__pycache__", "data", ".env*")


def _dependabot_docker_directories() -> dict[str, dict]:
    config = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    return {entry["directory"]: entry for entry in config["updates"] if entry["package-ecosystem"] == "docker"}


@unittest.skipUnless((REPO / "controller" / "Dockerfile").is_file(), "the repo's Dockerfiles are not in this tree")
class BaseImagesArePinnedTests(unittest.TestCase):
    def test_every_from_names_its_tag_and_a_digest(self) -> None:
        for context in CONTEXTS:
            text = (REPO / context / "Dockerfile").read_text(encoding="utf-8")
            bases = re.findall(r"^FROM\s+(\S+)", text, re.M)
            with self.subTest(dockerfile=f"{context or '.'}/Dockerfile"):
                self.assertTrue(bases)
                for base in bases:
                    self.assertRegex(base, f"^{PINNED.pattern}$")

    def test_dependabot_proposes_each_base_after_the_72_hour_cooldown(self) -> None:
        entries = _dependabot_docker_directories()
        for context in CONTEXTS:
            with self.subTest(directory=f"/{context}"):
                entry = entries.get(f"/{context}")
                self.assertIsNotNone(entry, f"no docker entry for /{context} in .github/dependabot.yml")
                self.assertGreaterEqual(entry.get("cooldown", {}).get("default-days", 0), 3)

    def test_each_build_context_keeps_the_checkout_s_secrets_and_caches_out(self) -> None:
        for context in CONTEXTS:
            ignore = REPO / context / ".dockerignore"
            with self.subTest(context=context or "."):
                self.assertTrue(ignore.is_file(), f"no {ignore.relative_to(REPO).as_posix()}")
                lines = ignore.read_text(encoding="utf-8").splitlines()
                self.assertLessEqual(set(KEPT_OUT), {line.strip().removeprefix("**/") for line in lines})
