"""What the server says an action approval covers (the webhook's and the SSE event's action_summary).

It named some fields and not others (not what a type types, nor whether it
replaces the field, nor how far a scroll goes), cut each at 200 characters
without saying so, and named a selector beside the element id that overrides
it. The dashboard had a list of its own. Both now render one list
(APPROVAL_FIELDS), which covers every field an approval binds.
"""

from __future__ import annotations

import tempfile
import typing
import unicodedata
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from app import approval_display
from app.approval_display import (
    ACTION_READS,
    APPROVAL_FIELDS,
    APPROVAL_FIELDS_SHOWN_OTHERWISE,
    UNBOUND_FIELDS,
    approval_summary,
    shown_code_points,
)
from app.approvals import ApprovalStore
from app.browser.services.actions import BrowserActionService
from app.models import ActionName, ApprovalRecord, BrowserActionDecision

NOW = "2026-10-27T12:00:00Z"
ELEMENT = "op-" + "a" * 24
AGENT_HID_TEXT = "text hidden at the agent's request (sensitive)"
# Never dispatched: refused as actions to approve or execute.
NOT_DISPATCHED = frozenset({"done", "request_human_takeover"})


def record(**action: Any) -> ApprovalRecord:
    return ApprovalRecord(
        id="appr-1",
        session_id="sess-1",
        kind="payment",
        status="pending",
        created_at=NOW,
        updated_at=NOW,
        reason="r",
        action=BrowserActionDecision(reason="r", **action),
    )


def changed(name: str, value: Any) -> Any:
    """Another value for the field ``name``, of its type."""
    if value is None:
        annotation = str(BrowserActionDecision.model_fields[name].annotation)
        return 1 if "float" in annotation or "int" in annotation else "changed"
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value + 1
    return f"changed-{value}"


class SummaryTests(unittest.TestCase):
    def test_the_element_id_that_runs_comes_before_the_selector_it_overrides(self) -> None:
        summary = approval_summary(record(action="click", selector="#cancel", element_id=ELEMENT))

        self.assertEqual(summary, f'click · element_id="{ELEMENT}" · selector="#cancel" (not used: element_id runs)')

    def test_a_field_the_action_does_not_read_is_marked_not_used(self) -> None:
        # Bound, so the approval covers it, but nothing reads it: shown as part of the action, a url read
        # as where the click goes, and coordinates over a visible button as what it clicks.
        click = approval_summary(record(action="click", selector="#confirm", url="https://harmless.example/settings"))
        press = approval_summary(record(action="press", key="Enter", file_path="report.pdf", text="hello"))
        navigate = approval_summary(record(action="navigate", url="https://bank.example/", recipient="mallory"))

        self.assertEqual(
            click, 'click · selector="#confirm" · url="https://harmless.example/settings" (not used by click)'
        )
        self.assertEqual(
            press, 'press · file_path="report.pdf" (not used by press) · key="Enter" · text="hello" (not used by press)'
        )
        self.assertEqual(
            navigate, 'navigate · url="https://bank.example/" · recipient="mallory" (not used by navigate)'
        )

    def test_a_field_another_field_overrides_says_which_one_runs(self) -> None:
        by_element = approval_summary(record(action="click", element_id=ELEMENT, x=120, y=640))
        by_selector = approval_summary(record(action="click", selector="form > button.danger", x=120, y=640))
        by_index = approval_summary(
            record(action="select_option", selector="#plan", value="basic", label="Premium", index=2)
        )
        by_value = approval_summary(record(action="select_option", selector="#plan", value="basic", label="Premium"))

        self.assertEqual(
            by_element,
            f'click · element_id="{ELEMENT}" · x=120 (not used: element_id runs) · y=640 (not used: element_id runs)',
        )
        self.assertEqual(
            by_selector,
            'click · selector="form > button.danger" · x=120 (not used: selector runs) · y=640 (not used: selector runs)',
        )
        self.assertEqual(
            by_index,
            'select_option · selector="#plan" · value="basic" (not used: index runs)'
            ' · label="Premium" (not used: index runs) · index=2',
        )
        self.assertEqual(
            by_value,
            'select_option · selector="#plan" · value="basic" (matches an option\'s value or label) · label="Premium" (not used: value runs)',
        )

    def test_an_empty_value_is_shown_and_runs_over_the_label(self) -> None:
        # Codex's round-4 case: value="" was left out as if unset, so the row read the label alone, and
        # select_option ran the empty value (any value that is not None runs before the label).
        shown = approval_summary(record(action="select_option", selector="#payment", value="", label="Cancel payment"))

        self.assertEqual(
            shown,
            'select_option · selector="#payment" · value="" (matches an option\'s value or label) · label="Cancel payment" (not used: value runs)',
        )

    def test_an_empty_label_is_shown(self) -> None:
        shown = approval_summary(record(action="select_option", selector="#payment", label=""))

        self.assertEqual(shown, 'select_option · selector="#payment" · label=""')

    def test_what_a_type_types_and_whether_it_replaces_the_field_are_shown(self) -> None:
        summary = approval_summary(
            record(action="type", selector="#comment", text="I resign,\neffective today", clear_first=False)
        )

        self.assertEqual(summary, 'type · selector="#comment" · text="I resign,\\neffective today" · clear_first=false')

    def test_a_sensitive_type_says_the_agent_hid_its_text_whatever_the_field(self) -> None:
        # The agent marks any field sensitive; the label says who hid the text, not that the field is a password.
        password = approval_summary(record(action="type", selector="#pw", text="hunter2", sensitive=True))
        comment = approval_summary(record(action="type", selector="#comment", text="I resign", sensitive=True))

        self.assertEqual(password, f'type · selector="#pw" · {AGENT_HID_TEXT} · clear_first=true')
        self.assertEqual(comment, f'type · selector="#comment" · {AGENT_HID_TEXT} · clear_first=true')
        self.assertNotIn("I resign", comment)

    def test_a_scroll_and_a_wait_say_how_far_and_how_long(self) -> None:
        scroll = approval_summary(record(action="scroll", delta_x=0, delta_y=-5000))
        wait = approval_summary(record(action="wait", wait_ms=1500))

        self.assertEqual((scroll, wait), ("scroll · delta_x=0 · delta_y=-5000", "wait · wait_ms=1500"))

    def test_an_integer_past_a_double_is_written_as_javascript_reads_it(self) -> None:
        # An index has no upper bound: 10**309 raised OverflowError in the summary (the SSE and webhook
        # path), and the agent got an internal error for an approval already stored. JSON.parse reads it
        # as Infinity, and one past 2**53 as the nearest double.
        huge = approval_summary(record(action="select_option", selector="#s", value="keep", index=10**309))
        past = approval_summary(record(action="select_option", selector="#s", index=2**53 + 1))

        self.assertEqual(huge, 'select_option · selector="#s" · value="keep" (not used: index runs) · index=Infinity')
        self.assertEqual(past, 'select_option · selector="#s" · index=9007199254740992')

    def test_a_number_is_written_as_the_dashboard_writes_it(self) -> None:
        # JavaScript's Number to string, which the queue row uses: Python wrote 1e-07, a 22-digit 1e21 and
        # 1e-05, where the row read 1e-7, 1e+21 and 0.00001. Expected values are node 22's String(n).
        cases = {
            1e-7: "1e-7",
            1e-5: "0.00001",
            1e21: "1e+21",
            1e20: "100000000000000000000",
            2.5: "2.5",
            -0.0: "0",
            123.456e-9: "1.23456e-7",
            5e-324: "5e-324",
            1.7976931348623157e308: "1.7976931348623157e+308",
            0.1 + 0.2: "0.30000000000000004",
            -1e-7: "-1e-7",
            1e300: "1e+300",
            640.0: "640",
        }
        for value, shown in cases.items():
            with self.subTest(value=value):
                self.assertEqual(
                    approval_summary(record(action="scroll", delta_x=value, delta_y=0)),
                    f"scroll · delta_x={shown} · delta_y=0",
                )

    def test_a_long_field_says_how_much_it_leaves_out(self) -> None:
        url = "https://bank.example/transfer?" + "a" * 230 + "&to=mallory&amount=9999"

        summary = approval_summary(record(action="navigate", url=url))

        self.assertEqual(summary, f'navigate · url="{url[:200]}"… ({len(url) - 200} more characters not shown)')

    def test_the_list_covers_every_field_an_approval_binds(self) -> None:
        shown = {name for name, _ in APPROVAL_FIELDS} | APPROVAL_FIELDS_SHOWN_OTHERWISE

        self.assertEqual(shown, set(BrowserActionDecision.model_fields) - UNBOUND_FIELDS)

    def test_an_approval_binds_every_field_but_the_unbound_ones(self) -> None:
        # What the list is checked against: the match that decides whether an approval covers an action.
        with tempfile.TemporaryDirectory() as root:
            store = ApprovalStore(root)
            base = BrowserActionDecision(action="type", reason="r", selector="#a", text="t")
            for name in BrowserActionDecision.model_fields:
                with self.subTest(field=name):
                    other = base.model_copy(update={name: changed(name, getattr(base, name))})
                    self.assertEqual(store._actions_match(base, other), name in UNBOUND_FIELDS)


class ReadMapTests(unittest.IsolatedAsyncioTestCase):
    """ACTION_READS against what _dispatch_decision passes on, so the notes cannot drift from what runs."""

    async def test_each_action_reads_what_the_map_says(self) -> None:
        calls: list[tuple[Any, ...]] = []

        async def recorded(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append((*args, *kwargs.values()))
            return {}

        methods = ("navigate", "click", "hover", "select_option", "type", "press", "scroll", "wait")
        methods += ("reload", "go_back", "go_forward", "upload")
        service = BrowserActionService(SimpleNamespace(**dict.fromkeys(methods, recorded)))
        shown = [name for name, _ in APPROVAL_FIELDS]
        for action in typing.get_args(ActionName):
            with self.subTest(action=action):
                # Each field a value of its own, so what reaches the manager names the fields it read.
                decision = BrowserActionDecision.model_construct(action=action, **{name: f"<{name}>" for name in shown})
                calls.clear()
                if action in NOT_DISPATCHED:
                    with self.assertRaises(ValueError):
                        await service._dispatch_decision("s", decision, approval_id=None)
                else:
                    await service._dispatch_decision("s", decision, approval_id=None)
                passed = {value for call in calls for value in call}
                self.assertEqual({name for name in shown if f"<{name}>" in passed}, set(ACTION_READS.get(action, ())))


class CodePointTests(unittest.TestCase):
    """What would hide or reorder text an operator reads is written as its code point, one category at a time.

    Only format characters were: a line separator ends a // comment in a
    script an operator approves, and a Hangul filler is an invisible name.
    """

    def test_format_characters(self) -> None:
        # U+FFF9 is a format character that is not default-ignorable: only the format category catches it.
        self.assertEqual(shown_code_points("a\u202eb\u200bc\ufff9d"), "a[U+202E]b[U+200B]c[U+FFF9]d")

    def test_a_format_character_newer_than_the_running_python_knows_is_still_shown(self) -> None:
        # The shipped image's Python is 3.11 (Unicode 14.0), where U+13439 (Egyptian hieroglyph format
        # control, Unicode 15.0) is unassigned: the dashboard escaped it and the server did not.
        real = unicodedata.category

        class Unicode14:
            @staticmethod
            def category(char: str) -> str:
                return "Cn" if 0x13439 <= ord(char) <= 0x1343F else real(char)

        with patch.object(approval_display, "unicodedata", Unicode14()):
            self.assertEqual(shown_code_points("a\U00013439b"), "a[U+13439]b")

    def test_the_format_table_holds_every_format_character_the_running_python_knows(self) -> None:
        table = approval_display._FORMAT_CHARACTERS
        unknown = [
            f"U+{code:04X}"
            for code in range(0x110000)
            if unicodedata.category(chr(code)) == "Cf" and not any(low <= code <= high for low, high in table)
        ]

        self.assertEqual(unknown, [], f"Unicode {unicodedata.unidata_version} has format characters the table lacks")

    def test_an_unassigned_code_point_is_shown_as_it_is(self) -> None:
        # Decided (round 4): only what the table or the running Python names as hiding is escaped, as the
        # dashboard does. U+0378 is unassigned in every Unicode version so far.
        self.assertEqual(shown_code_points("a\u0378b"), "a\u0378b")

    def test_control_characters_and_a_newline(self) -> None:
        self.assertEqual(shown_code_points("a\nb\x1bc\u0085d"), "a\\nb[U+001B]c[U+0085]d")

    def test_a_line_separator(self) -> None:
        self.assertEqual(shown_code_points("// log only\u2028send()"), "// log only[U+2028]send()")

    def test_a_paragraph_separator(self) -> None:
        self.assertEqual(shown_code_points("a\u2029b"), "a[U+2029]b")

    def test_default_ignorable_code_points(self) -> None:
        # A variation selector, Hangul fillers, a combining grapheme joiner and a tag character.
        self.assertEqual(
            shown_code_points("a\ufe0fb\u3164c\u115fd\u034fe\U000e0041f"),
            "a[U+FE0F]b[U+3164]c[U+115F]d[U+034F]e[U+E0041]f",
        )

    def test_letters_of_other_scripts_stay_readable(self) -> None:
        # The limit: a Cyrillic а that reads as a Latin a, and right-to-left letters, are shown as they are.
        self.assertEqual(shown_code_points("аpi.bank.example שלום"), "аpi.bank.example שלום")
