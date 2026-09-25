"""A governed workflow cannot skip approval by calling a write "read".

`risk_category` is written by whoever produced the decision, and in an agent
run that is a model reading the page it is acting on. The governed profile
exempted anything labelled "read", so a click on "Confirm payment" that the
model (or text on the page steering it) called "read" ran with no approval.
"""

from __future__ import annotations

import unittest

from app.browser.services.actions import BrowserActionService
from app.models import BrowserActionDecision

kind_for = BrowserActionService.governed_approval_kind_for_decision


def decision(action: str, **fields) -> BrowserActionDecision:
    return BrowserActionDecision(action=action, reason="test", **fields)


class GovernedReadLabelTests(unittest.TestCase):
    def test_actions_that_change_nothing_may_be_read(self) -> None:
        for action, fields in (
            ("navigate", {"url": "https://example.com"}),
            ("hover", {"selector": "#menu"}),
            ("scroll", {}),
            ("wait", {}),
            ("reload", {}),
            ("go_back", {}),
            ("go_forward", {}),
            ("done", {}),
        ):
            with self.subTest(action=action):
                self.assertIsNone(kind_for(decision(action, risk_category="read", **fields)))

    def test_a_write_labelled_read_still_needs_approval(self) -> None:
        for action, fields in (
            ("click", {"selector": "#confirm-payment"}),
            ("type", {"selector": "#amount", "text": "5000"}),
            ("press", {"key": "Enter"}),
            ("select_option", {"selector": "#plan", "value": "premium"}),
        ):
            with self.subTest(action=action):
                self.assertEqual(kind_for(decision(action, risk_category="read", **fields)), "write")

    def test_an_upload_labelled_read_is_an_upload(self) -> None:
        upload = decision("upload", selector="#file", file_path="a.pdf", risk_category="read")
        self.assertEqual(kind_for(upload), "upload")

    def test_higher_risk_labels_are_kept(self) -> None:
        self.assertEqual(kind_for(decision("click", selector="#buy", risk_category="payment")), "payment")


if __name__ == "__main__":
    unittest.main()
