"""The reviewed SKILL.md renders contract text as literals, so a goal cannot restructure the document."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.harness import Postcondition, Precondition, TaskContract
from app.harness.induce import SkillInducer, _md_literal
from app.harness.trace import TraceRecorder
from app.harness.verifier.base import VerificationResult

HOSTILE = "Open it\n\n# Injected heading\n<img src=x onerror=alert(1)>\n```\n[click](javascript:alert(1))"
HEADINGS = [
    "# hostile",
    "## Goal",
    "## Preconditions",
    "## Postconditions",
    "## Verification",
    "## Runtime",
]


def _skill_markdown(root: Path) -> str:
    contract = TaskContract(
        id="hostile",
        goal=HOSTILE,
        preconditions=[Precondition(kind="note", value=HOSTILE)],
        postconditions=[Postcondition(kind="text_contains", value=HOSTILE)],
    )
    recorder = TraceRecorder(root / "trace", run_id="r1", contract_hash=contract.hash())
    trace = recorder.finalize(final_observation={"url": "https://example.com/"}, metadata={"mode": "mock"})
    verification = VerificationResult(passed=True, confidence=1.0, notes=HOSTILE)
    candidate = SkillInducer(root / "staging").induce(
        contract=contract, trace=trace, verification=verification, attempts=1
    )
    return Path(candidate.files["SKILL.md"]).read_text(encoding="utf-8")


class SkillMarkdownTests(unittest.TestCase):
    def test_contract_text_cannot_add_headings_or_html_to_the_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            markdown = _skill_markdown(Path(tmp))

        body = markdown.split("\n---\n", 1)[1]
        self.assertEqual([line for line in body.splitlines() if line.startswith("#")], HEADINGS)
        for line in body.splitlines():
            if "<img" in line or "javascript:" in line:
                self.assertRegex(line, r"(^|: )(`+) .* \2$")
        # No fenced block is opened: a line that starts with backticks closes them on the same line.
        for line in body.splitlines():
            if line.startswith("```"):
                self.assertRegex(line, r"^(`+) .* \1$")

    def test_literal_uses_a_fence_longer_than_any_backtick_run_in_the_value(self) -> None:
        self.assertEqual(_md_literal("plain"), "` plain `")
        self.assertEqual(_md_literal("a ``` b"), "```` a ``` b ````")
        self.assertEqual(_md_literal("one\ntwo three"), "` one two three `")
        self.assertEqual(_md_literal("   "), "(empty)")


if __name__ == "__main__":
    unittest.main()
