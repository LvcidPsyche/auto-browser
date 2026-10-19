"""The exported script replays what happened, in order, from where the session started.

The exporter read the audit log in the wrong order, read action values from a
shape the action pipeline does not write, and began at the page the session was
on when it was exported. Events here go through the real AuditStore (which lists
newest first) and are built with the pipeline's own target builders; the same
export on a real Chromium session is in test_playwright_export_pipeline.py.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app import playwright_export
from app.audit import AuditStore
from app.browser.services.actions import BrowserActionService
from app.playwright_export import build_script, export_session_script

SESSION = "sess-1"
START = "https://shop.example/login"
ELEMENT_NOTE = "was tagged by auto-browser's observe() and does not exist in a replay: replace this selector"


async def make_store(tmp_path: Path, events: list[tuple[str, str, str, dict[str, Any]]]) -> AuditStore:
    """An AuditStore holding (event_type, status, action, details) rows, oldest first."""
    store = AuditStore(tmp_path / "audit")
    await store.startup()
    for event_type, status, action, details in events:
        await store.append(event_type=event_type, status=status, action=action, session_id=SESSION, details=details)
    return store


def created(start_url: str) -> tuple[str, str, str, dict[str, Any]]:
    return (
        "session_created",
        "ok",
        "create_session",
        {"start_url": start_url, "isolation_mode": "shared_browser_node"},
    )


def acted(action: str, target: dict[str, Any], status: str = "ok") -> tuple[str, str, str, dict[str, Any]]:
    """An action row as the pipeline writes it: details={"target": ..., "verification": ...}."""
    return ("browser_action", status, action, {"target": target, "verification": {"verified": True}})


def body_lines(script: str) -> list[str]:
    """The replayed statements and comments, without the boilerplate around them."""
    lines = script.splitlines()
    first = lines.index("        page = context.new_page()") + 1
    last = lines.index("        context.close()")
    return [line.strip() for line in lines[first:last] if line.strip()]


def assert_plain_module(script: str) -> ast.Module:
    """Compiles, and holds only the docstring, the import, run() and the main guard."""
    compile(script, "replay.py", "exec")
    tree = ast.parse(script)
    assert [type(node) for node in tree.body] == [ast.Expr, ast.ImportFrom, ast.FunctionDef, ast.If]
    return tree


def replayed_statements(tree: ast.Module) -> list[str]:
    """What run() does between opening the page and closing the browser."""
    run = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    with_body = run.body[0].body  # type: ignore[attr-defined]
    return [ast.unparse(node) for node in with_body[3:-2]]


async def test_events_replay_oldest_first_from_the_recorded_start_url(tmp_path: Path) -> None:
    store = await make_store(
        tmp_path,
        [
            created(START),
            acted("navigate", {"url": "https://shop.example/cart"}),
            ("observe", "ok", "observe", {}),
            acted("click", {"mode": "selector", "selector": "#pay", "x": 10.5, "y": 20.5}),
            acted("type", {"mode": "selector", "selector": "#email", "clear_first": True, "text_preview": "a@b.co"}),
            acted("press", {"key": "Enter"}),
            acted("click", {"mode": "selector", "selector": "#never"}, status="failed"),
            acted("navigate", {"url": "https://shop.example/done"}),
        ],
    )

    export = await export_session_script(SESSION, store)

    assert body_lines(export["script"]) == [
        "page.goto('https://shop.example/login')",
        "page.wait_for_load_state('domcontentloaded')",
        "page.goto('https://shop.example/cart')",
        "page.locator('#pay').first.click()",
        "page.locator('#email').first.clear()",
        "page.locator('#email').first.fill('a@b.co')",
        "page.keyboard.press('Enter')",
        "page.goto('https://shop.example/done')",
    ]
    assert "# Start URL: https://shop.example/login" in export["script"]
    assert export["truncated"] is False
    assert export["action_count"] == 5
    assert_plain_module(export["script"])


async def test_without_a_session_created_event_the_script_has_no_initial_goto(tmp_path: Path) -> None:
    store = await make_store(tmp_path, [acted("press", {"key": "Tab"})])

    export = await export_session_script(SESSION, store)

    assert body_lines(export["script"]) == ["page.keyboard.press('Tab')"]
    assert "# Start URL: (not recorded)" in export["script"]


async def test_a_first_navigation_to_the_start_url_is_not_repeated(tmp_path: Path) -> None:
    store = await make_store(
        tmp_path, [created(START), acted("navigate", {"url": START}), acted("press", {"key": "A"})]
    )

    export = await export_session_script(SESSION, store)

    assert body_lines(export["script"]) == [
        "page.goto('https://shop.example/login')",
        "page.wait_for_load_state('domcontentloaded')",
        "page.keyboard.press('A')",
    ]


async def test_a_log_cut_at_the_limit_says_so_in_the_script_and_the_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(playwright_export, "EXPORT_EVENT_LIMIT", 3)
    store = await make_store(tmp_path, [acted("press", {"key": f"k{i}"}) for i in range(1, 6)])

    export = await export_session_script(SESSION, store)

    assert export["truncated"] is True
    assert export["event_count"] == 3
    lines = body_lines(export["script"])
    assert lines[:2] == [
        "# NOTE: the audit log was cut off at 3 events, so the oldest steps of this session are",
        "# missing and this script does not start where the session did.",
    ]
    # The newest three survive, still oldest first.
    assert lines[2:] == ["page.keyboard.press('k3')", "page.keyboard.press('k4')", "page.keyboard.press('k5')"]
    assert_plain_module(export["script"])


async def test_a_log_exactly_at_the_limit_is_not_called_cut(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(playwright_export, "EXPORT_EVENT_LIMIT", 3)
    store = await make_store(tmp_path, [acted("press", {"key": f"k{i}"}) for i in range(1, 4)])

    export = await export_session_script(SESSION, store)

    assert export["truncated"] is False
    assert "NOTE: the audit log was cut off" not in export["script"]
    assert body_lines(export["script"]) == [f"page.keyboard.press('k{i}')" for i in range(1, 4)]


def test_a_truncated_log_with_no_actions_still_compiles() -> None:
    script = build_script(SESSION, [], truncated=True)

    assert_plain_module(script)
    assert "pass" in body_lines(script)


HOSTILE_URLS = [
    pytest.param('https://e.test/"""\nimport os\nos.system("x")\n"""', id="triple-quote"),
    pytest.param("https://e.test/?q=\\N{bogus}", id="backslash-N-escape"),
    pytest.param("https://e.test/\r\nimport os\r\nos.system('x')", id="crlf"),
    pytest.param("https://e.test/\rimport os", id="cr"),
    pytest.param("https://e.test/ import os", id="unicode-line-separator"),
    pytest.param("https://e.test/\x00x", id="nul"),
]


@pytest.mark.parametrize("start_url", HOSTILE_URLS)
async def test_a_hostile_start_url_cannot_break_or_extend_the_script(tmp_path: Path, start_url: str) -> None:
    store = await make_store(tmp_path, [created(start_url), acted("press", {"key": "Tab"})])

    export = await export_session_script(SESSION, store)

    tree = assert_plain_module(export["script"])
    assert replayed_statements(tree) == [
        f"page.goto({start_url!r})",
        "page.wait_for_load_state('domcontentloaded')",
        "page.keyboard.press('Tab')",
    ]
    # The URL is on one comment line and nowhere else in the header.
    header = export["script"].split("from playwright", 1)[0]
    comment = [line for line in header.splitlines() if line.startswith("# Start URL:")]
    assert len(comment) == 1
    assert not any(line.lstrip().startswith("import os") for line in header.splitlines())
    assert ast.get_docstring(tree) is not None and "e.test" not in ast.get_docstring(tree)


@pytest.mark.parametrize(
    ("action", "extra", "expected"),
    [
        ("click", {}, "page.locator('[data-operator-id=\"op-7\"]').first.click()"),
        ("hover", {}, "page.locator('[data-operator-id=\"op-7\"]').first.hover()"),
        (
            "select_option",
            {"value": "large"},
            "page.locator('[data-operator-id=\"op-7\"]').first.select_option(value='large')",
        ),
    ],
)
def test_an_element_id_target_is_exported_with_a_note_that_it_will_not_exist(
    action: str, extra: dict[str, Any], expected: str
) -> None:
    target = {**BrowserActionService.resolve_target(element_id="op-7"), **extra}
    script = build_script(
        SESSION, [{"event_type": "browser_action", "status": "ok", "action": action, "details": {"target": target}}]
    )

    assert body_lines(script) == [f"{expected}  # element_id op-7 {ELEMENT_NOTE}"]
    assert_plain_module(script)


def test_a_typed_element_id_target_notes_both_lines_and_keeps_the_redaction_note() -> None:
    target = BrowserActionService.text_target_payload(
        BrowserActionService.resolve_target(element_id="op-9"),
        "hunter2",
        clear_first=True,
        sensitive=True,
        preview_chars=80,
    )
    script = build_script(
        SESSION, [{"event_type": "browser_action", "status": "ok", "action": "type", "details": {"target": target}}]
    )

    locator = "page.locator('[data-operator-id=\"op-9\"]').first"
    note = f"element_id op-9 {ELEMENT_NOTE}"
    assert body_lines(script) == [
        f"{locator}.clear()  # {note}",
        f"{locator}.fill('<REDACTED>')  # {note}; text was marked sensitive and redacted",
    ]
    assert "hunter2" not in script


def test_a_selector_target_has_no_element_note() -> None:
    target = BrowserActionService.resolve_target(selector="#go")
    script = build_script(
        SESSION, [{"event_type": "browser_action", "status": "ok", "action": "click", "details": {"target": target}}]
    )

    assert body_lines(script) == ["page.locator('#go').first.click()"]


def test_a_hostile_element_id_cannot_end_the_note_comment() -> None:
    target = BrowserActionService.resolve_target(element_id="op-1\nimport os\nos.system('x')")
    script = build_script(
        SESSION, [{"event_type": "browser_action", "status": "ok", "action": "click", "details": {"target": target}}]
    )

    tree = assert_plain_module(script)
    assert len(replayed_statements(tree)) == 1
    assert not any(line.lstrip().startswith("import os") for line in script.splitlines())


def test_an_event_without_a_usable_target_is_skipped_loudly_not_replayed_empty() -> None:
    # The old flat shape, and a target with nothing to act on.
    events = [
        {"event_type": "browser_action", "status": "ok", "action": "click", "details": {"selector": "#old"}},
        {"event_type": "browser_action", "status": "ok", "action": "navigate", "details": {"target": {}}},
    ]

    script = build_script(SESSION, events)

    assert body_lines(script) == [
        "# skipped click: the audit event has no selector",
        "# skipped navigate: the audit event has no url",
        "# No recorded actions found in this session",
        "pass",
    ]
    assert_plain_module(script)
