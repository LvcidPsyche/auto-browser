"""
playwright_export.py — Generate a runnable Playwright Python script from
a session's audit log.

The export reads action events from the AuditStore for a given session and
synthesises a self-contained Python script that reproduces the same steps.
The script uses the sync Playwright API for readability.

Supported action types (mapped from audit log action field):
  navigate, click, hover, type, press, scroll, wait, reload,
  go_back, go_forward, select_option, open_tab, activate_tab, close_tab

Unsupported/skipped: observe, screenshot, approval management.
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from typing import Any

_SCRIPT_HEADER = '''\
#!/usr/bin/env python3
"""
Auto-Browser session export — {session_id}
Generated: {timestamp}
Sessions: {url}

Run:
    pip install playwright
    playwright install chromium
    python {filename}
"""

from playwright.sync_api import sync_playwright


def run() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={{"width": {viewport_w}, "height": {viewport_h}}})
        page = context.new_page()

'''

_SCRIPT_FOOTER = '''\
        context.close()
        browser.close()


if __name__ == "__main__":
    run()
'''


def _indent(code: str, spaces: int = 8) -> str:
    return textwrap.indent(code, " " * spaces)


def _action_to_code(action: str, details: dict[str, Any]) -> str | None:
    """Convert a single audit action to a Playwright Python statement."""
    if action == "navigate":
        url = details.get("url", "")
        return f'page.goto({url!r})\n'

    if action == "click":
        mode = details.get("mode")
        if mode == "coordinates":
            x, y = details.get("x", 0), details.get("y", 0)
            return f"page.mouse.click({x}, {y})\n"
        selector = details.get("selector", "")
        return f'page.locator({selector!r}).first.click()\n'

    if action == "hover":
        mode = details.get("mode")
        if mode == "coordinates":
            x, y = details.get("x", 0), details.get("y", 0)
            return f"page.mouse.move({x}, {y})\n"
        selector = details.get("selector", "")
        return f'page.locator({selector!r}).first.hover()\n'

    if action == "type":
        selector = details.get("selector", "")
        # Use placeholder if text was redacted
        if details.get("text_redacted"):
            text = "<REDACTED>"
            comment = "  # text was marked sensitive and redacted\n"
        else:
            text = details.get("text_preview", "")
            comment = ""
        clear_first = details.get("clear_first", True)
        loc = f'page.locator({selector!r}).first'
        lines = ""
        if clear_first:
            lines += f'{loc}.clear()\n'
        lines += f'{loc}.fill({text!r}){comment}\n' if not comment else f'{loc}.fill({text!r}){comment}'
        return lines

    if action == "press":
        key = details.get("key", "")
        return f'page.keyboard.press({key!r})\n'

    if action == "scroll":
        dx = details.get("delta_x", 0)
        dy = details.get("delta_y", 0)
        return f"page.mouse.wheel({dx}, {dy})\n"

    if action == "wait":
        ms = details.get("wait_ms", 1000)
        return f"page.wait_for_timeout({ms})\n"

    if action == "reload":
        return "page.reload()\n"

    if action == "go_back":
        return "page.go_back()\n"

    if action == "go_forward":
        return "page.go_forward()\n"

    if action == "select_option":
        selector = details.get("selector", "")
        value = details.get("value")
        label = details.get("label")
        index = details.get("index")
        if value is not None:
            return f'page.locator({selector!r}).first.select_option(value={value!r})\n'
        if label is not None:
            return f'page.locator({selector!r}).first.select_option(label={label!r})\n'
        if index is not None:
            return f'page.locator({selector!r}).first.select_option(index={index!r})\n'
        return None

    if action == "open_tab":
        url = details.get("url")
        if url:
            return f'tab = context.new_page()\ntab.goto({url!r})\npage = tab\n'
        return "page = context.new_page()\n"

    if action == "upload":
        selector = details.get("selector", "")
        file_path = details.get("file_path", "<path/to/file>")
        return f'page.locator({selector!r}).first.set_input_files({file_path!r})  # verify file path before running\n'

    return None  # unsupported — skip


def build_script(
    session_id: str,
    audit_events: list[dict[str, Any]],
    *,
    start_url: str = "",
    viewport_w: int = 1280,
    viewport_h: int = 800,
    filename: str = "session_replay.py",
) -> str:
    """
    Build a runnable Playwright Python script from a list of audit events.

    Args:
        session_id:    Session ID (for header comment).
        audit_events:  List of audit event dicts from AuditStore.
                       Each dict needs: action, status, details.
        start_url:     Optional starting URL to navigate to first.
        viewport_w/h:  Viewport dimensions for the new_context call.
        filename:      Filename to put in the script header comment.

    Returns:
        A Python script as a string.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    header = _SCRIPT_HEADER.format(
        session_id=session_id,
        timestamp=timestamp,
        url=start_url or "(unknown)",
        filename=filename,
        viewport_w=viewport_w,
        viewport_h=viewport_h,
    )

    body_lines: list[str] = []

    if start_url:
        body_lines.append(f"page.goto({start_url!r})\n")
        body_lines.append("page.wait_for_load_state('domcontentloaded')\n")

    first_navigate_skipped = False
    for event in audit_events:
        event_type = event.get("event_type", "")
        action = event.get("action", "")
        status = event.get("status", "")
        details = event.get("details") or {}

        # Skip non-action events and failed actions
        if event_type not in {"browser_action", "action"}:
            continue
        if status not in {"ok", "success", "completed"}:
            continue

        # Skip the first navigate action if it matches start_url to avoid duplicating goto
        if action == "navigate" and not first_navigate_skipped and start_url:
            if details.get("url", "").rstrip("/") == start_url.rstrip("/"):
                first_navigate_skipped = True
                continue
        first_navigate_skipped = True

        code = _action_to_code(action, details)
        if code:
            body_lines.append(code)

    if not body_lines:
        body_lines.append("# No recorded actions found in this session\n")
        body_lines.append("pass\n")

    body = "".join(body_lines)
    indented_body = _indent(body, 8)

    return header + indented_body + "\n" + _SCRIPT_FOOTER


async def export_session_script(
    session_id: str,
    audit_store: Any,
    *,
    start_url: str = "",
    viewport_w: int = 1280,
    viewport_h: int = 800,
) -> dict[str, Any]:
    """
    Fetch audit events for session_id and return a Playwright export script.

    Returns:
        {
          "session_id": str,
          "script": str,
          "event_count": int,
          "action_count": int,
        }
    """
    events_raw = await audit_store.list(session_id=session_id, limit=5000) if hasattr(audit_store, "list") else []
    events = [e.model_dump() if hasattr(e, "model_dump") else e for e in events_raw]
    script = build_script(
        session_id=session_id,
        audit_events=events,
        start_url=start_url,
        viewport_w=viewport_w,
        viewport_h=viewport_h,
        filename=f"{session_id}_replay.py",
    )
    action_events = [
        e for e in events
        if e.get("event_type") in {"browser_action", "action"}
        and e.get("status") in {"ok", "success", "completed"}
    ]
    return {
        "session_id": session_id,
        "script": script,
        "event_count": len(events),
        "action_count": len(action_events),
    }
