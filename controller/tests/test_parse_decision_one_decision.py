"""A reply must hold one decision: two different ones are refused, not chosen between.

`parse_decision_text` took the first decision-shaped object anywhere in the text, then the last, so JSON the model
quoted from the page (`{"action": "click", "selector": "#delete-account"}`) could beat the decision the model gave by
where it stood. The prompt asks for exactly one decision, so a reply holding two different ones (fenced, bare, or in
one wrapper) fails as an unparseable reply does. The same decision given twice is one decision.
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
UNPARSEABLE = "did not return a valid BrowserActionDecision"


class ParseDecisionOneDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OpenAIAdapter(Settings(_env_file=None))

    def parse(self, text: str):
        return self.adapter.parse_decision_text(text)

    def assertRefused(self, text: str) -> None:
        with self.assertRaises(RuntimeError) as caught:
            self.parse(text)
        self.assertNotIsInstance(caught.exception, RecursionError)
        self.assertIn(UNPARSEABLE, str(caught.exception))

    def test_a_fenced_page_quote_and_a_bare_decision_are_refused(self) -> None:
        self.assertRefused(f"The page shows:\n```json\n{json.dumps(PAGE)}\n```\nMy decision:\n{json.dumps(OWN)}")

    def test_a_bare_page_quote_and_a_fenced_decision_are_refused(self) -> None:
        self.assertRefused(f"The page contained {json.dumps(PAGE)}.\nMy decision:\n```json\n{json.dumps(OWN)}\n```")

    def test_two_different_decisions_in_prose_are_refused_in_either_order(self) -> None:
        for first, second in ((PAGE, OWN), (OWN, PAGE)):
            with self.subTest(first=first["action"]):
                self.assertRefused(f"The page contained this text: {json.dumps(first)}\nThen: {json.dumps(second)}")

    def test_an_unclosed_quoted_fence_and_a_fenced_decision_are_refused(self) -> None:
        self.assertRefused(f"The page says: ```json\n{json.dumps(PAGE)}\nThen mine:\n```json\n{json.dumps(OWN)}\n```")

    def test_a_wrapper_with_two_different_decisions_is_refused(self) -> None:
        for wrapper in ({"decision": OWN, "page_said": PAGE}, {"page_said": PAGE, "decision": OWN}, [PAGE, OWN]):
            for text in (json.dumps(wrapper), f"result: {json.dumps(wrapper)}"):
                with self.subTest(text=text):
                    self.assertRefused(text)

    def test_the_same_decision_fenced_and_bare_is_accepted(self) -> None:
        text = f"```json\n{json.dumps(OWN)}\n```\nTo repeat: {json.dumps(OWN)}"

        self.assertEqual(self.parse(text).action, "done")

    def test_a_decision_object_inside_a_wrapper_is_found_once(self) -> None:
        self.assertEqual(self.parse(f"result: {json.dumps({'decision': OWN})}").action, "done")
        self.assertEqual(self.parse(json.dumps({"decision": OWN, "notes": {"seen": "a form"}})).action, "done")

    def test_a_single_decision_and_a_bare_decision_still_parse(self) -> None:
        self.assertEqual(self.parse(json.dumps(OWN)).action, "done")
        self.assertEqual(self.parse(f"thinking... {json.dumps(OWN)} thanks").action, "done")

    def test_deeply_nested_brackets_are_refused_as_unparseable(self) -> None:
        depth = 200_000
        for text in ("[" * depth, '{"a": ' + "[" * depth + "]" * depth + "}"):
            with self.subTest(prefix=text[:8]):
                self.assertRefused(text)


if __name__ == "__main__":
    unittest.main()
