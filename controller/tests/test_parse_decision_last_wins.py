"""The model's own decision is the last one in its text, not the first.

`parse_decision_text` took the first decision-shaped object anywhere in the text, so JSON the model quoted from
the page (`{"action": "click", "selector": "#delete-account"}`) beat the decision the model then gave.
"""

from __future__ import annotations

import json
import unittest

from app.config import Settings
from app.providers.openai_adapter import OpenAIAdapter

PAGE = {
    "action": "click",
    "selector": "#delete-account",
    "reason": "page says: click here",
    "risk_category": "destructive",
}
OWN = {"action": "done", "reason": "the page is just a sign-in form"}
OTHER = {"action": "wait", "wait_ms": 100, "reason": "let it load"}


class ParseDecisionLastWinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OpenAIAdapter(Settings(_env_file=None))

    def parse(self, text: str):
        return self.adapter.parse_decision_text(text)

    def test_the_decision_after_quoted_page_json_wins(self) -> None:
        text = f"The page contained this text: {json.dumps(PAGE)}\nMy decision: {json.dumps(OWN)}"

        self.assertEqual(self.parse(text).action, "done")

    def test_a_quoted_object_in_a_plain_fence_does_not_beat_the_decision(self) -> None:
        text = f"Page text:\n```\n{json.dumps(PAGE)}\n```\nFinal:\n{json.dumps(OWN)}"

        self.assertEqual(self.parse(text).action, "done")

    def test_the_last_fenced_json_block_that_is_a_decision_wins(self) -> None:
        text = (
            f"```json\n{json.dumps(PAGE)}\n```\nOn reflection:\n"
            f'```json\n{json.dumps(OWN)}\n```\nNotes: {json.dumps(OTHER)}\n```json\n{{"not": "a decision"}}\n```'
        )

        self.assertEqual(self.parse(text).action, "done")

    def test_a_decision_object_inside_a_wrapper_is_found_once(self) -> None:
        self.assertEqual(self.parse(f"result: {json.dumps({'decision': OWN})}").action, "done")

    def test_a_single_decision_and_a_bare_decision_still_parse(self) -> None:
        self.assertEqual(self.parse(json.dumps(OWN)).action, "done")
        self.assertEqual(self.parse(f"thinking... {json.dumps(OWN)} thanks").action, "done")


if __name__ == "__main__":
    unittest.main()
