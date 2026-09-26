import unittest

from app.actions.pipeline import classify_playwright_error


class ClassifyPlaywrightErrorTests(unittest.TestCase):
    def test_target_not_found_markers(self) -> None:
        messages = [
            "Timeout 30000ms exceeded while waiting for locator('#submit')",
            "strict mode violation: locator resolved to 2 elements",
            "Error: element is not attached to the DOM",
            "Element is not attached to the DOM",
            "locator resolved to 0 elements",
            "Timeout while waiting for selector \"#missing\"",
        ]
        for message in messages:
            with self.subTest(message=message):
                code, text = classify_playwright_error(message)
                self.assertEqual(code, "target_not_found")
                self.assertEqual(
                    text,
                    "The element is no longer on the page. Observe again and pick a current element.",
                )

    def test_target_not_visible_markers(self) -> None:
        messages = [
            "element is not visible",
            "Element is not visible",
            "Timeout exceeded: element is outside of the viewport",
            "waiting for element to be stable... element is not stable",
        ]
        for message in messages:
            with self.subTest(message=message):
                code, text = classify_playwright_error(message)
                self.assertEqual(code, "target_not_visible")
                self.assertEqual(text, "The element is on the page but not visible or not stable.")

    def test_click_intercepted_markers(self) -> None:
        messages = [
            "<div id=overlay>…</div> intercepts pointer events",
            "subtree intercepts pointer events",
            "element would receive the click instead of the target",
        ]
        for message in messages:
            with self.subTest(message=message):
                code, text = classify_playwright_error(message)
                self.assertEqual(code, "click_intercepted")
                self.assertEqual(text, "Another element covers the target.")

    def test_unrecognized_message_stays_generic(self) -> None:
        for message in ["Target page, context or browser has been closed", "detached", "", None]:
            with self.subTest(message=message):
                code, text = classify_playwright_error(message)
                self.assertEqual(code, "browser_action_failed")
                self.assertEqual(text, "Action failed. Refresh observation and retry.")

    def test_a_real_timeout_call_log_names_what_actually_stopped_it(self) -> None:
        # Playwright's timeout message always starts with "waiting for locator";
        # the later call-log lines carry the real cause.
        head = "Timeout 30000ms exceeded.\nCall log:\n  - waiting for locator('#go').first\n"
        intercepted = (
            head
            + '  - locator resolved to <button id="go">Go</button>\n'
            + '  - <div class="backdrop"></div> intercepts pointer events\n'
        )
        hidden = head + "  - element is not visible\n"
        missing = "Timeout 30000ms exceeded.\nCall log:\n  - waiting for locator('[data-operator-id=\"op-x\"]').first\n"
        self.assertEqual(classify_playwright_error(intercepted)[0], "click_intercepted")
        self.assertEqual(classify_playwright_error(hidden)[0], "target_not_visible")
        self.assertEqual(classify_playwright_error(missing)[0], "target_not_found")

    def test_case_insensitive(self) -> None:
        code, _ = classify_playwright_error("ELEMENT IS NOT ATTACHED TO THE DOM")
        self.assertEqual(code, "target_not_found")


if __name__ == "__main__":
    unittest.main()
