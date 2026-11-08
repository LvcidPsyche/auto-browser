from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from app.playwright_export import _action_to_code, _indent, build_script, export_session_script


class PlaywrightExportHelperTests(unittest.TestCase):
    def test_indent_adds_padding(self) -> None:
        indented = _indent("line1\nline2\n", 4)
        self.assertEqual(indented, "    line1\n    line2\n")

    def test_action_to_code_covers_supported_actions(self) -> None:
        cases: list[tuple[str, dict[str, Any], str | None]] = [
            ("navigate", {"url": "https://example.com"}, "page.goto('https://example.com')\n"),
            ("click", {"mode": "coordinates", "x": 10, "y": 20}, "page.mouse.click(10, 20)\n"),
            ("click", {"selector": "#submit"}, "page.locator('#submit').first.click()\n"),
            ("hover", {"mode": "coordinates", "x": 30, "y": 40}, "page.mouse.move(30, 40)\n"),
            ("hover", {"selector": "#menu"}, "page.locator('#menu').first.hover()\n"),
            ("press", {"key": "Enter"}, "page.keyboard.press('Enter')\n"),
            ("scroll", {"delta_x": 2, "delta_y": 30}, "page.mouse.wheel(2, 30)\n"),
            ("wait", {"wait_ms": 1500}, "page.wait_for_timeout(1500)\n"),
            ("reload", {}, "page.reload()\n"),
            ("go_back", {}, "page.go_back()\n"),
            ("go_forward", {}, "page.go_forward()\n"),
            (
                "select_option",
                {"selector": "#size", "value": "large"},
                "page.locator('#size').first.select_option(value='large')\n",
            ),
            (
                "select_option",
                {"selector": "#size", "label": "Large"},
                "page.locator('#size').first.select_option(label='Large')\n",
            ),
            (
                "select_option",
                {"selector": "#size", "index": 2},
                "page.locator('#size').first.select_option(index=2)\n",
            ),
            ("select_option", {"selector": "#size"}, None),
            (
                "open_tab",
                {"url": "https://example.com/new"},
                "tab = context.new_page()\ntab.goto('https://example.com/new')\npage = tab\n",
            ),
            ("open_tab", {}, "page = context.new_page()\n"),
            (
                "upload",
                {"selector": "input[type=file]", "file_path": "demo.pdf"},
                "page.locator('input[type=file]').first.set_input_files('demo.pdf')  "
                "# verify file path before running\n",
            ),
            ("unsupported", {}, None),
        ]

        for action, target, expected in cases:
            with self.subTest(action=action, target=target):
                self.assertEqual(_action_to_code(action, {"target": target}), expected)

    def test_action_to_code_handles_redacted_and_plain_type(self) -> None:
        plain = _action_to_code(
            "type",
            {"target": {"selector": "#email", "text_preview": "alice@example.com", "clear_first": False}},
        )
        redacted = _action_to_code(
            "type",
            {"target": {"selector": "#password", "text_redacted": True, "clear_first": True}},
        )

        self.assertEqual(plain, "page.locator('#email').first.fill('alice@example.com')\n")
        self.assertEqual(
            redacted,
            "page.locator('#password').first.clear()\n"
            "page.locator('#password').first.fill('<REDACTED>')  "
            "# text was marked sensitive and redacted\n",
        )

    def test_a_number_field_holding_anything_but_a_number_is_never_written_into_the_script(self) -> None:
        """ACT-31: x, y, the scroll amounts and wait_ms went into the script as they were, unlike every string (written
        with repr), so a target holding code where a number belongs wrote that code into a script an operator runs."""
        code = "1); __import__('os').system('id'); ("
        cases = [
            ("click", {"mode": "coordinates", "x": code, "y": 20}, "numeric coordinates"),
            ("hover", {"mode": "coordinates", "x": 30, "y": code}, "numeric coordinates"),
            ("click", {"mode": "coordinates", "x": float("inf"), "y": 20}, "numeric coordinates"),
            ("scroll", {"delta_x": code, "delta_y": 30}, "numeric scroll amounts"),
            ("scroll", {"delta_x": 2, "delta_y": float("nan")}, "numeric scroll amounts"),
            ("wait", {"wait_ms": code}, "numeric wait time"),
            ("wait", {"wait_ms": True}, "numeric wait time"),
        ]

        for action, target, missing in cases:
            with self.subTest(action=action, target=target):
                self.assertEqual(
                    _action_to_code(action, {"target": target}),
                    f"# skipped {action}: the audit event has no {missing}\n",
                )

    def test_a_typed_text_cut_to_its_preview_says_so(self) -> None:
        """ACT-31: the audit log keeps only the start of a long text, which the script typed as if it were all."""
        target = {"selector": "#note", "text_preview": "draft", "text_truncated": True, "clear_first": False}

        self.assertEqual(
            _action_to_code("type", {"target": target}),
            "page.locator('#note').first.fill('draft')  # the audit log kept only the start of the text typed: "
            "complete it\n",
        )

    def test_build_script_includes_header_and_skips_duplicate_start_navigation(self) -> None:
        script = build_script(
            "session-123",
            [
                {
                    "event_type": "browser_action",
                    "action": "navigate",
                    "status": "ok",
                    "details": {"target": {"url": "https://example.com"}},
                },
                {
                    "event_type": "browser_action",
                    "action": "click",
                    "status": "completed",
                    "details": {"target": {"selector": "#submit"}},
                },
                {
                    "event_type": "observe",
                    "action": "observe",
                    "status": "ok",
                    "details": {},
                },
                {
                    "event_type": "browser_action",
                    "action": "reload",
                    "status": "failed",
                    "details": {},
                },
            ],
            start_url="https://example.com",
            filename="demo.py",
        )

        self.assertIn("Auto-Browser session export", script)
        self.assertIn("session-123", script)
        self.assertEqual(script.count("page.goto('https://example.com')"), 1)
        self.assertIn("page.wait_for_load_state('domcontentloaded')", script)
        self.assertIn("page.locator('#submit').first.click()", script)
        self.assertNotIn("page.reload()", script)

    def test_build_script_without_actions_emits_placeholder(self) -> None:
        script = build_script("empty-session", [], start_url="")
        self.assertIn("# No recorded actions found in this session", script)
        self.assertIn("pass", script)


class PlaywrightExportAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_session_script_returns_counts_and_script(self) -> None:
        class EventModel:
            def __init__(self, payload: dict[str, Any]) -> None:
                self.payload = payload

            def model_dump(self) -> dict[str, Any]:
                return self.payload

        audit_store = SimpleNamespace(
            list=AsyncMock(
                return_value=[
                    EventModel(
                        {
                            "event_type": "browser_action",
                            "action": "navigate",
                            "status": "ok",
                            "details": {"target": {"url": "https://example.com"}},
                        }
                    ),
                    EventModel(
                        {
                            "event_type": "browser_action",
                            "action": "click",
                            "status": "success",
                            "details": {"target": {"selector": "#submit"}},
                        }
                    ),
                    EventModel(
                        {
                            "event_type": "browser_action",
                            "action": "reload",
                            "status": "failed",
                            "details": {},
                        }
                    ),
                ]
            )
        )

        export = await export_session_script(
            "session-456",
            audit_store,
            viewport_w=1440,
            viewport_h=900,
        )

        self.assertEqual(export["session_id"], "session-456")
        self.assertEqual(export["event_count"], 3)
        self.assertEqual(export["action_count"], 2)
        self.assertIn("session-456_replay.py", export["script"])
        self.assertIn('viewport={"width": 1440, "height": 900}', export["script"])
