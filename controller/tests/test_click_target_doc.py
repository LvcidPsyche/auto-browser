"""Guard: the click classifier's phrases in docs/production-hardening.md are the module's, no more and no fewer.

The page listed some of the table as examples, so an operator could not tell from it which button asks for approval
in the fast profile, and nothing made the list follow the table. Each kind's phrases, the harmless continuations
and the words that make "Send" harmless are now listed whole, and read back here.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from app.browser.click_target import CLICK_LABEL_PHRASES, HARMLESS_AFTER_SEND, HARMLESS_LABEL_PHRASES

_DOC = Path(__file__).resolve().parents[2] / "docs" / "production-hardening.md"
# Where the classifier's section starts, and what each of its lists starts with.
_SECTION = "- In the fast profile a click's `risk_category` is the model's word"
_HARMLESS = "- Not asked about, though each starts with a phrase above:"
_AFTER_SEND = "- Nor is `Send` asked about when its line goes on to one of:"


def _listed(section: str, start: str) -> list[str]:
    """The phrases in backticks of the bullet that starts with ``start``, lower-cased, up to the next bullet;
    none when the section has no such bullet."""
    lines = section.splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip().startswith(start)), None)
    if first is None:
        return []
    bullet = [lines[first].strip()[len(start) :]]
    for line in lines[first + 1 :]:
        if line.strip().startswith("- "):
            break
        bullet.append(line.strip())
    return [phrase.casefold() for phrase in re.findall(r"`([^`]+)`", " ".join(bullet))]


class ClickTargetDocTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _DOC.exists():  # repo layout absent in the image
            self.skipTest(f"doc-sync source unavailable: {_DOC}")
        text = _DOC.read_text(encoding="utf-8")
        start = text.index(_SECTION)
        self.section = text[start : text.index("\n- ", start)]

    def test_each_kinds_phrases_are_the_tables(self) -> None:
        for kind, phrases in CLICK_LABEL_PHRASES.items():
            with self.subTest(kind=kind):
                self.assertEqual(sorted(_listed(self.section, f"- `{kind}`:")), sorted(phrases))

    def test_the_harmless_continuations_are_the_modules(self) -> None:
        self.assertEqual(sorted(_listed(self.section, _HARMLESS)), sorted(HARMLESS_LABEL_PHRASES))
        self.assertEqual(sorted(_listed(self.section, _AFTER_SEND)), sorted(HARMLESS_AFTER_SEND))


if __name__ == "__main__":
    unittest.main()
