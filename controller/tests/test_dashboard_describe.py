"""What the approvals queue shows for an action, run in node: the operator approves what it says.

The real-browser dashboard test needs a local Chromium and skips without one;
these run the dashboard's own functions, taken from the page, in node. The
queue renders the server's field list (app.approval_display.APPROVAL_FIELDS), so it
says what the webhook and SSE summary say, field for field.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest

from app.approval_display import approval_summary, shown_code_points
from app.models import ApprovalRecord, BrowserActionDecision
from app.routes.extensions import _DASHBOARD_HTML
from app.tool_gateway.gateway import McpToolGateway
from app.tool_inputs import EvalJsInput

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="no node binary")
# The page's own declarations, in the order they depend on each other.
DECLARATIONS = (
    "APPROVAL_DISPLAY",
    "asText",
    "shownText",
    "cutChars",
    "leftOut",
    "shownValue",
    "WRITTEN_HOST",
    "urlAsSent",
    "shownField",
    "describeAction",
    "SERVER_CUT_NOTE",
    "cutPreview",
    "approvalConfirmText",
    "argumentsText",
    "formatEventTime",
    "approvalCells",
)
NODE_TIMEOUT_SECONDS = 60
CUT = 200
ELEMENT = "op-" + "a" * 24
AGENT_HID_TEXT = "text hidden at the agent's request (sensitive)"


def _declaration(name: str) -> str:
    start = _DASHBOARD_HTML.index(f"const {name} = ")
    line_end = _DASHBOARD_HTML.index("\n", start)
    if _DASHBOARD_HTML[line_end - 1] != "{":
        return _DASHBOARD_HTML[start : line_end + 1]
    return _DASHBOARD_HTML[start : _DASHBOARD_HTML.index("\n};\n", start) + 3]


def in_node(expression: str, given: Any, names: tuple[str, ...] = DECLARATIONS) -> Any:
    """``expression`` evaluated in node over ``input`` (``given``), with the page's declarations in scope."""
    script = "\n".join(_declaration(name) for name in names) + (
        f"\nconst input = JSON.parse(process.argv[1]);\nprocess.stdout.write(JSON.stringify({expression}));"
    )
    result = subprocess.run(
        [NODE, "-e", script, json.dumps(given)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=NODE_TIMEOUT_SECONDS,
        check=True,
    )
    return json.loads(result.stdout)


def describe(*cases: tuple[dict[str, Any], str | None], cut: int | None = None) -> list[str]:
    return in_node(
        "input.cases.map(([action, origin]) => describeAction(action, origin, input.cut ?? undefined))",
        {"cases": cases, "cut": cut},
    )


def test_format_characters_in_an_action_are_shown_as_code_points() -> None:
    (shown,) = describe(({"action": "click", "selector": "#pay\u202e\u200b"}, None))

    assert shown == 'click · selector="#pay[U+202E][U+200B]"'


def test_an_empty_value_is_shown_and_runs_over_the_label() -> None:
    # Codex's round-4 case: the row read 'select_option · selector="#payment" · label="Cancel payment"',
    # and select_option ran value="".
    (shown,) = describe(
        ({"action": "select_option", "selector": "#payment", "value": "", "label": "Cancel payment"}, None)
    )

    assert (
        shown
        == 'select_option · selector="#payment" · value="" (matches an option\'s value or label) · label="Cancel payment" (not used: value runs)'
    )


def test_every_field_an_approval_binds_is_shown() -> None:
    # The approval covers these exactly (_actions_match), so the operator must see them.
    shown = describe(
        ({"action": "click", "x": 640, "y": 410}, None),
        ({"action": "select_option", "element_id": "e12", "value": "transfer-all"}, None),
        ({"action": "select_option", "selector": "#plan", "label": "Gold"}, None),
        ({"action": "select_option", "selector": "#plan", "index": 0}, None),
        ({"action": "scroll", "delta_x": 0, "delta_y": 600}, None),
        ({"action": "type", "selector": "#note", "text": "hi", "clear_first": False}, None),
        ({"action": "type", "selector": "#note", "text": "hi", "clear_first": True}, None),
        ({"action": "wait", "wait_ms": 1500}, None),
    )

    assert shown == [
        "click · x=640 · y=410",
        'select_option · element_id="e12" · value="transfer-all" (matches an option\'s value or label)',
        'select_option · selector="#plan" · label="Gold"',
        'select_option · selector="#plan" · index=0',
        "scroll · delta_x=0 · delta_y=600",
        'type · selector="#note" · text="hi" · clear_first=false',
        'type · selector="#note" · text="hi" · clear_first=true',
        "wait · wait_ms=1500",
    ]


def test_a_sensitive_type_says_the_agent_hid_its_text_whatever_the_field() -> None:
    # The agent marks any field sensitive; the label says who hid the text, not that the field is a password.
    password, comment = describe(
        ({"action": "type", "selector": "#pw", "text": "hunter2", "sensitive": True}, None),
        ({"action": "type", "selector": "#comment", "text": "I resign", "sensitive": True}, None),
    )

    assert password == f'type · selector="#pw" · {AGENT_HID_TEXT}'
    assert comment == f'type · selector="#comment" · {AGENT_HID_TEXT}'


def test_the_element_id_that_runs_comes_before_the_selector_it_overrides() -> None:
    (shown,) = describe(({"action": "click", "selector": "#cancel", "element_id": ELEMENT}, None))

    assert shown == f'click · element_id="{ELEMENT}" · selector="#cancel" (not used: element_id runs)'


def test_a_field_that_does_not_act_is_marked_not_used() -> None:
    by_selector, by_action = describe(
        ({"action": "click", "selector": "form > button", "x": 120, "y": 640}, None),
        ({"action": "press", "key": "Enter", "url": "https://harmless.example/"}, None),
    )

    assert (
        by_selector
        == 'click · selector="form > button" · x=120 (not used: selector runs) · y=640 (not used: selector runs)'
    )
    assert by_action == 'press · url="https://harmless.example/" (not used by press) · key="Enter"'


def test_a_newline_in_a_field_is_shown_as_an_escape() -> None:
    (shown,) = describe(({"action": "select_option", "selector": "select#plan\n\n\n, select#payout", "index": 1}, None))

    assert shown == 'select_option · selector="select#plan\\n\\n\\n, select#payout" · index=1'


def test_the_row_cuts_nothing_and_a_cut_says_how_much_it_leaves_out() -> None:
    text = "\U0001f600" + "x" * 299  # 300 characters, the first outside the BMP
    action = {"action": "type", "selector": "#note", "text": text, "clear_first": True}

    (row,) = describe((action, None))
    (cut,) = describe((action, None), cut=CUT)

    assert row == f'type · selector="#note" · text={json.dumps(text, ensure_ascii=False)} · clear_first=true'
    kept = json.dumps(text[:CUT], ensure_ascii=False)
    assert cut == f'type · selector="#note" · text={kept}… (100 more characters not shown) · clear_first=true'


def test_a_tool_call_is_named_by_the_server_origin_only() -> None:
    stand_in = {"action": "request_human_takeover", "text": "browser.eval_js sha256:" + "ab" * 32}

    tool_call, forged = describe((stand_in, "tool_call"), (stand_in, "action"))

    assert tool_call == "tool call browser.eval_js (arguments under Reason)"
    assert forged.startswith('request_human_takeover · text="browser.eval_js sha256:')


def test_the_confirm_dialog_is_short_names_the_target_first_and_points_to_the_row() -> None:
    approval = {
        "kind": "post",
        "session_id": "sess-1",
        "origin": "action",
        "action": {"action": "type", "selector": "#note", "text": "x" * 300, "clear_first": True},
        "reason": "\u202e" + "y" * 250,
    }

    message = in_node("approvalConfirmText(input)", approval)

    assert message.startswith('Approve this post action in session sess-1?\n\ntype · selector="#note" · text=')
    assert f'text="{"x" * CUT}"… (100 more characters not shown)' in message
    assert f"Reason, as the agent wrote it: [U+202E]{'y' * (CUT - 1)}… (51 more characters not shown)" in message
    assert "\u202e" not in message
    assert message.endswith("The queue row shows the whole approval.")


def test_a_tool_calls_dialog_labels_the_servers_preview_and_points_to_the_whole_call() -> None:
    # Its reason is the server's preview of the arguments, not the agent's words, and the row shows only
    # that preview: the whole call is in the arguments viewer.
    approval = {
        "kind": "write",
        "session_id": "sess-1",
        "origin": "tool_call",
        "arguments_size": 935,
        "action": {"action": "request_human_takeover", "text": "browser.eval_js sha256:" + "ab" * 32},
        "reason": 'Approve browser.eval_js with {"expression": "pay()"}',
    }
    unsized = {key: value for key, value in approval.items() if key != "arguments_size"}  # asked before 1.11.2

    message, before = in_node("input.map((given) => approvalConfirmText(given))", [approval, unsized])

    assert 'Arguments (server preview): Approve browser.eval_js with {"expression": "pay()"}' in message
    assert "as the agent wrote it" not in message
    # PDT-7: the viewer leaves out what the preview does (a cookie's value, a storage value's text).
    assert message.endswith("View all 935 characters in the queue shows the whole call, credentials left out.")
    assert before.endswith("Nothing more of the call is kept than this preview.")
    assert "The queue row shows the whole approval" not in message + before


def test_a_tool_calls_dialog_keeps_the_servers_count_when_it_cuts_the_preview() -> None:
    # OV4-6: the dialog cut the server's preview at 200 characters with a count of its own ("454 more
    # characters not shown") beside "View all 2100 characters": two totals for one call, and the
    # server's note cut away.
    class EvalJs:
        name = "browser.eval_js"
        governed_kind = "write"  # the stand-in's risk category reads it

    arguments = EvalJsInput(session_id="s", expression="x" * 2000)
    size = len(McpToolGateway._governed_call_arguments(EvalJs(), arguments))
    stand_in, reason = McpToolGateway._governed_call_decision(EvalJs(), arguments, arguments_size=size)
    approval = {
        "kind": "write",
        "session_id": "s",
        "origin": "tool_call",
        "arguments_size": size,
        "action": stand_in.model_dump(mode="json"),
        "reason": reason,
    }

    (message,) = in_node("input.map((given) => approvalConfirmText(given))", [approval])

    note = f"({size} characters in all: View all shows them)"
    assert reason.endswith(note)
    assert note in message
    assert "more characters not shown" not in message
    shown = message.split("Arguments (server preview): ", 1)[1].split(note, 1)[0]
    assert len(shown) <= CUT + len("… "), shown


def test_after_a_decision_the_queue_says_what_the_server_said() -> None:
    # A reject that waits on a claimed action answers 200 with the record still executed and a message;
    # the queue read "Approval a executed.", which hid that the rejection stops the waiting action.
    deferred = {
        "status": "executed",
        "message": "approval a is claimed by an action that has not begun: the rejection stops the action",
    }
    plain = {"status": "approved"}

    said = in_node(
        "input.map((result) => decisionStatusText('a', result))",
        [deferred, plain],
        (*DECLARATIONS, "decisionStatusText"),
    )

    assert said == [deferred["message"], "Approval a approved."]


def test_the_queue_and_the_server_summary_say_the_same() -> None:
    actions = [
        BrowserActionDecision(action="click", reason="r", selector="#cancel", element_id=ELEMENT),
        BrowserActionDecision(action="click", reason="r", x=640, y=410.5),
        BrowserActionDecision(action="type", reason="r", selector="#m", text="a\nb\u202e" + "c" * 300),
        BrowserActionDecision(action="type", reason="r", selector="#pw", text="hunter2", sensitive=True),
        BrowserActionDecision(action="scroll", reason="r", delta_x=0, delta_y=-5000),
        BrowserActionDecision(action="wait", reason="r", wait_ms=1500),
        BrowserActionDecision(action="navigate", reason="r", url="https://bank.example/?" + "q" * 250),
        BrowserActionDecision(action="select_option", reason="r", element_id="e12", index=0),
        BrowserActionDecision(action="upload", reason="r", selector="#file", file_path="a b.txt"),
        BrowserActionDecision(action="press", reason="r", key="Enter"),
        # Fields the action does not read, and fields another overrides (item 8 of round 4).
        BrowserActionDecision(action="click", reason="r", selector="#confirm", url="https://harmless.example/"),
        BrowserActionDecision(action="click", reason="r", element_id=ELEMENT, x=120, y=640),
        BrowserActionDecision(action="click", reason="r", selector="form > button", x=120, y=640),
        BrowserActionDecision(action="press", reason="r", key="Enter", file_path="r.pdf", text="hello"),
        BrowserActionDecision(action="select_option", reason="r", selector="#p", value="v", label="L", index=2),
        BrowserActionDecision(action="select_option", reason="r", selector="#p", value="v", label="L"),
        # An empty value runs, over the label (round 5, item 3), and an empty label is shown.
        BrowserActionDecision(
            action="select_option", reason="r", selector="#payment", value="", label="Cancel payment"
        ),
        BrowserActionDecision(action="select_option", reason="r", selector="#p", value="", index=1),
        BrowserActionDecision(action="select_option", reason="r", selector="#p", label=""),
        BrowserActionDecision(action="navigate", reason="r", url="https://bank.example/", recipient="mallory"),
        BrowserActionDecision(action="type", reason="r", selector="#pw", text="x", sensitive=True, x=1, y=2),
        # Numbers as JavaScript writes them (round 4, item 15).
        BrowserActionDecision(action="scroll", reason="r", delta_x=1e-7, delta_y=1e21),
        BrowserActionDecision(action="click", reason="r", x=1e-5, y=1e300),
        # An integer past a double (round 5, item 10): JSON.parse reads Infinity, or the nearest double.
        BrowserActionDecision(action="select_option", reason="r", selector="#s", value="keep", index=10**309),
        BrowserActionDecision(action="select_option", reason="r", selector="#s", index=2**53 + 1),
    ]
    records = [
        ApprovalRecord(
            id=f"a{index}",
            session_id="s",
            kind="write",
            status="pending",
            created_at="t",
            updated_at="t",
            reason="r",
            action=action,
        )
        for index, action in enumerate(actions)
    ]

    queue = describe(*[(record.model_dump(mode="json")["action"], record.origin) for record in records], cut=CUT)

    assert queue == [approval_summary(record) for record in records]


def test_a_navigate_urls_host_is_shown_as_a_browser_sends_it() -> None:
    """B-DISP-4: a navigate approval showed url="https://аpple.com/" (a Cyrillic а) as written, and the browser
    loads xn--pple-43d.com. The host a browser sends is shown, as the tool-call preview shows it, with the host as
    written beside it; an ASCII host is shown as it is."""
    urls = [
        "https://аpple.com/login",
        "https://u@bücher.example:8443/?q=1",
        "https://bank.example/",
    ]
    expected = [
        'navigate · url="https://xn--pple-43d.com/login" (host as written: аpple.com)',
        'navigate · url="https://u@xn--bcher-kva.example:8443/?q=1" (host as written: bücher.example)',
        'navigate · url="https://bank.example/"',
    ]
    records = [
        ApprovalRecord(
            id=f"a{index}",
            session_id="s",
            kind="write",
            status="pending",
            created_at="t",
            updated_at="t",
            reason="r",
            action=BrowserActionDecision(action="navigate", reason="r", url=url),
        )
        for index, url in enumerate(urls)
    ]

    queue = describe(*[(record.model_dump(mode="json")["action"], record.origin) for record in records], cut=CUT)

    assert [approval_summary(record) for record in records] == expected
    assert queue == expected


# One string per category the page writes as code points; the server's shown_code_points must agree.
HIDDEN = {
    # U+FFF9 is a format character that is not default-ignorable: only the format category catches it.
    "format (a bidi override, an annotation anchor)": ("a\u202eb\u200bc\ufff9d", "a[U+202E]b[U+200B]c[U+FFF9]d"),
    # Unicode 15.0: unassigned where the server's Python is 3.11, so the server's table must hold it.
    "format, newer than Unicode 14 (a hieroglyph format control)": ("a\U00013439b", "a[U+13439]b"),
    "control (a newline, an escape)": ("a\nb\x1bc\u0085d", "a\\nb[U+001B]c[U+0085]d"),
    "line separator": ("// log only\u2028send()", "// log only[U+2028]send()"),
    "paragraph separator": ("a\u2029b", "a[U+2029]b"),
    "default ignorable": (
        "a\ufe0fb\u3164c\u115fd\u034fe\U000e0041f",
        "a[U+FE0F]b[U+3164]c[U+115F]d[U+034F]e[U+E0041]f",
    ),
    "other scripts stay readable": ("аpi שלום", "аpi שלום"),
}


@pytest.mark.parametrize("category", list(HIDDEN))
def test_the_page_writes_each_hidden_category_as_code_points(category: str) -> None:
    given, shown = HIDDEN[category]

    assert in_node("shownText(input)", given) == shown


def test_the_page_and_the_server_write_the_same_code_points() -> None:
    strings = [given for given, _ in HIDDEN.values()]

    assert in_node("input.map((value) => shownText(value))", strings) == [shown_code_points(value) for value in strings]


def test_a_queue_row_shows_the_reason_with_what_hides_in_it() -> None:
    approval = {
        "id": "appr-click5",
        "created_at": None,
        "session_id": "sess-123456789abc",
        "kind": "payment",
        "origin": "action",
        "action": {"action": "click", "x": 640, "y": 410},
        "reason": "Pay \u202eto mallory\u2028now",
    }

    cells = in_node("approvalCells(input).map(([text]) => text)", approval)

    # The approval's id, which the arguments viewer's title names.
    assert cells == [
        "—",
        "appr-click5",
        "sess-1234567",
        "payment",
        "click · x=640 · y=410",
        "Pay [U+202E]to mallory[U+2028]now",
    ]


def test_the_arguments_viewer_keeps_the_lines_and_shows_what_hides_in_them() -> None:
    # The server writes ASCII JSON; the viewer holds even what it would not write.
    given = '{\n  "expression": "pay(/*\u202e*/)\u2028x"\n}'

    assert in_node("argumentsText(input)", given) == '{\n  "expression": "pay(/*[U+202E]*/)[U+2028]x"\n}'
