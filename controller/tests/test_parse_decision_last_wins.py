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

    def test_the_last_decision_by_position_wins_fenced_or_not(self) -> None:
        # Round 4: a fenced decision no longer beats a bare one that comes after it.
        text = (
            f"```json\n{json.dumps(PAGE)}\n```\nOn reflection:\n"
            f'```json\n{json.dumps(OWN)}\n```\nNotes: {json.dumps(OTHER)}\n```json\n{{"not": "a decision"}}\n```'
        )

        self.assertEqual(self.parse(text).action, "wait")

    def test_a_bare_decision_after_a_fenced_page_quote_wins(self) -> None:
        # The prompt asks for bare JSON, so a fenced block in the reply is more likely quoted page text.
        text = f"The page shows:\n```json\n{json.dumps(PAGE)}\n```\nMy decision:\n{json.dumps(OWN)}"

        self.assertEqual(self.parse(text).action, "done")

    def test_a_fenced_decision_after_a_bare_page_quote_wins(self) -> None:
        text = f"The page contained {json.dumps(PAGE)}.\nMy decision:\n```json\n{json.dumps(OWN)}\n```"

        self.assertEqual(self.parse(text).action, "done")

    def test_an_unclosed_quoted_fence_does_not_hide_the_models_fenced_decision(self) -> None:
        text = f"The page says: ```json\n{json.dumps(PAGE)}\nThen mine:\n```json\n{json.dumps(OWN)}\n```"

        self.assertEqual(self.parse(text).action, "done")

    def test_a_decision_object_inside_a_wrapper_is_found_once(self) -> None:
        self.assertEqual(self.parse(f"result: {json.dumps({'decision': OWN})}").action, "done")

    def test_the_last_of_several_decisions_in_one_json_value_wins(self) -> None:
        wrapper = json.dumps({"page_said": PAGE, "decision": OWN})
        array = json.dumps([PAGE, OWN])
        for text in (wrapper, array, f"result: {wrapper}", f"result: {array}"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).action, "done")

    def test_a_single_decision_and_a_bare_decision_still_parse(self) -> None:
        self.assertEqual(self.parse(json.dumps(OWN)).action, "done")
        self.assertEqual(self.parse(f"thinking... {json.dumps(OWN)} thanks").action, "done")


if __name__ == "__main__":
    unittest.main()
