"""
playwright_export.py — Generate a runnable Playwright Python script from
a session's audit log.

The export reads action events from the AuditStore for a given session and
synthesises a self-contained Python script that reproduces the same steps.
The script uses the sync Playwright API for readability.

The action pipeline records each action as ``details={"target": {...}}``
(``resolve_target`` and ``text_target_payload`` in ``browser/services/actions.py``
build the target), so every value below is read from ``details["target"]``.

Supported action types (mapped from audit log action field):
  navigate, click, hover, type, press, scroll, wait, reload,
  go_back, go_forward, select_option, open_tab, upload

Unsupported/skipped: observe, screenshot, approval management, tab switches.
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from typing import Any

# How many audit events one export reads. The log is read newest first, so a
# session with more events than this loses its oldest ones; the script says so.
EXPORT_EVENT_LIMIT = 5000

_OK_STATUSES = {"ok", "success", "completed"}
_ACTION_EVENT_TYPES = {"browser_action", "action"}

_SCRIPT_HEADER = '''\
#!/usr/bin/env python3
"""
Auto-Browser session export — {session_id}
Generated: {timestamp}

Run:
    pip install playwright
    playwright install chromium
    python {filename}
"""

# Start URL: {url}

from playwright.sync_api import sync_playwright


def run() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={{"width": {viewport_w}, "height": {viewport_h}}})
        page = context.new_page()

'''

_SCRIPT_FOOTER = """\
        context.close()
        browser.close()


if __name__ == "__main__":
    run()
"""

_TRUNCATED_NOTE = (
    "# NOTE: the audit log was cut off at {limit} events, so the oldest steps of this session are\n"
    "# missing and this script does not start where the session did.\n"
)


def _indent(code: str, spaces: int = 8) -> str:
    return textwrap.indent(code, " " * spaces)


def _one_line(value: Any) -> str:
    """Text that cannot end a ``#`` comment: line breaks and other control characters become spaces.

    The value comes from a request (a start URL, an element id). A line break in
    it would otherwise end the comment and the rest would run as code.
    """
    return "".join(ch if ch.isprintable() else " " for ch in str(value))


def _line(code: str, *notes: str) -> str:
    comment = f"  # {'; '.join(notes)}" if notes else ""
    return f"{code}{comment}\n"


def _skipped(action: str, missing: str) -> str:
    return f"# skipped {action}: the audit event has no {missing}\n"


def _locator(target: dict[str, Any]) -> tuple[str, tuple[str, ...]] | None:
    """The locator expression for a selector target, with the notes that belong on every line using it."""
    selector = target.get("selector")
    if not isinstance(selector, str) or not selector:
        return None
    loc = f"page.locator({selector!r}).first"
    element_id = target.get("element_id")
    if not element_id:
        return loc, ()
    # The id names an element in the controller's private map of the live page
    # (app/browser/element_ids.py), and the selector is only its hint: no page,
    # live or replayed, carries that attribute.
    note = (
        f"element_id {_one_line(element_id)} was tagged by auto-browser's observe() "
        "and does not exist in a replay: replace this selector"
    )
    return loc, (note,)


def _action_to_code(action: str, details: dict[str, Any]) -> str | None:
    """Convert a single audit action to a Playwright Python statement."""
    target = details.get("target")
    if not isinstance(target, dict):
        target = {}

    if action == "navigate":
        url = target.get("url")
        if not url:
            return _skipped(action, "url")
        return f"page.goto({url!r})\n"

    if action in {"click", "hover"}:
        if target.get("mode") == "coordinates":
            x, y = target.get("x"), target.get("y")
            if x is None or y is None:
                return _skipped(action, "coordinates")
            call = "mouse.click" if action == "click" else "mouse.move"
            return f"page.{call}({x}, {y})\n"
        located = _locator(target)
        if located is None:
            return _skipped(action, "selector")
        loc, notes = located
        return _line(f"{loc}.{action}()", *notes)

    if action == "type":
        located = _locator(target)
        if located is None:
            return _skipped(action, "selector")
        loc, notes = located
        # Use placeholder if text was redacted
        if target.get("text_redacted"):
            text = "<REDACTED>"
            fill_notes = (*notes, "text was marked sensitive and redacted")
        else:
            text = target.get("text_preview", "")
            fill_notes = notes
        lines = ""
        if target.get("clear_first", True):
            lines += _line(f"{loc}.clear()", *notes)
        return lines + _line(f"{loc}.fill({text!r})", *fill_notes)

    if action == "press":
        key = target.get("key")
        if not key:
            return _skipped(action, "key")
        return f"page.keyboard.press({key!r})\n"

    if action == "scroll":
        return f"page.mouse.wheel({target.get('delta_x', 0)}, {target.get('delta_y', 0)})\n"

    if action == "wait":
        return f"page.wait_for_timeout({target.get('wait_ms', 1000)})\n"

    if action == "reload":
        return "page.reload()\n"

    if action == "go_back":
        return "page.go_back()\n"

    if action == "go_forward":
        return "page.go_forward()\n"

    if action == "select_option":
        located = _locator(target)
        if located is None:
            return _skipped(action, "selector")
        loc, notes = located
        for name in ("value", "label", "index"):
            if target.get(name) is not None:
                return _line(f"{loc}.select_option({name}={target[name]!r})", *notes)
        return None

    if action == "open_tab":
        url = target.get("url")
        if url:
            return f"tab = context.new_page()\ntab.goto({url!r})\npage = tab\n"
        return "page = context.new_page()\n"

    if action == "upload":
        located = _locator(target)
        if located is None:
            return _skipped(action, "selector")
        loc, notes = located
        file_path = target.get("file_path", "<path/to/file>")
        return _line(f"{loc}.set_input_files({file_path!r})", *notes, "verify file path before running")

    return None  # unsupported — skip


def _is_replayable(event: dict[str, Any]) -> bool:
    """A finished browser action; observations, approvals and failed actions are not replayed."""
    return event.get("event_type") in _ACTION_EVENT_TYPES and event.get("status") in _OK_STATUSES


def session_start_url(events_oldest_first: list[dict[str, Any]]) -> str:
    """Where the session began, from its ``session_created`` event; "" when that event is gone.

    The page's address at export time is where the session is now, not where it
    started, so it cannot stand in for this.
    """
    for event in events_oldest_first:
        if event.get("event_type") == "session_created":
            start_url = (event.get("details") or {}).get("start_url")
            return start_url if isinstance(start_url, str) else ""
    return ""


def build_script(
    session_id: str,
    audit_events: list[dict[str, Any]],
    *,
    start_url: str = "",
    viewport_w: int = 1280,
    viewport_h: int = 800,
    filename: str = "session_replay.py",
    truncated: bool = False,
) -> str:
    """
    Build a runnable Playwright Python script from a list of audit events.

    Args:
        session_id:    Session ID (for header comment).
        audit_events:  Audit event dicts from AuditStore, OLDEST FIRST.
                       Each dict needs: event_type, action, status, details.
        start_url:     Optional starting URL to navigate to first.
        viewport_w/h:  Viewport dimensions for the new_context call.
        filename:      Filename to put in the script header comment.
        truncated:     The oldest events were cut off; the script says so.

    Returns:
        A Python script as a string.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    header = _SCRIPT_HEADER.format(
        session_id=session_id,
        timestamp=timestamp,
        url=_one_line(start_url) or "(not recorded)",
        filename=filename,
        viewport_w=viewport_w,
        viewport_h=viewport_h,
    )

    body_lines: list[str] = []

    if truncated:
        body_lines.append(_TRUNCATED_NOTE.format(limit=EXPORT_EVENT_LIMIT))

    if start_url:
        body_lines.append(f"page.goto({start_url!r})\n")
        body_lines.append("page.wait_for_load_state('domcontentloaded')\n")

    first_navigate_skipped = False
    for event in audit_events:
        if not _is_replayable(event):
            continue
        action = event.get("action", "")
        details = event.get("details") or {}

        # Skip the first navigate action if it matches start_url to avoid duplicating goto
        if action == "navigate" and not first_navigate_skipped and start_url:
            navigated_to = (details.get("target") or {}).get("url") or ""
            if navigated_to.rstrip("/") == start_url.rstrip("/"):
                first_navigate_skipped = True
                continue
        first_navigate_skipped = True

        code = _action_to_code(action, details)
        if code:
            body_lines.append(code)

    # A block holding only comments does not compile, so it needs a statement.
    if all(line.lstrip().startswith("#") for chunk in body_lines for line in chunk.splitlines()):
        body_lines.append("# No recorded actions found in this session\n")
        body_lines.append("pass\n")

    body = "".join(body_lines)
    indented_body = _indent(body, 8)

    return header + indented_body + "\n" + _SCRIPT_FOOTER


async def export_session_script(
    session_id: str,
    audit_store: Any,
    *,
    viewport_w: int = 1280,
    viewport_h: int = 800,
) -> dict[str, Any]:
    """
    Fetch audit events for session_id and return a Playwright export script.

    The store lists newest first; the script replays oldest first. At most
    EXPORT_EVENT_LIMIT events are read, and when the session has more, the
    oldest are the ones missing: the script and the result both say so.

    Returns:
        {
          "session_id": str,
          "script": str,
          "event_count": int,
          "action_count": int,
          "truncated": bool,
        }
    """
    newest_first = (
        await audit_store.list(session_id=session_id, limit=EXPORT_EVENT_LIMIT + 1)
        if hasattr(audit_store, "list")
        else []
    )
    # One more than the limit is asked for: getting it is how a cut is told from an exact fit.
    truncated = len(newest_first) > EXPORT_EVENT_LIMIT
    events = [e.model_dump() if hasattr(e, "model_dump") else e for e in reversed(newest_first[:EXPORT_EVENT_LIMIT])]
    script = build_script(
        session_id=session_id,
        audit_events=events,
        start_url=session_start_url(events),
        viewport_w=viewport_w,
        viewport_h=viewport_h,
        filename=f"{session_id}_replay.py",
        truncated=truncated,
    )
    return {
        "session_id": session_id,
        "script": script,
        "event_count": len(events),
        "action_count": sum(1 for e in events if _is_replayable(e)),
        "truncated": truncated,
    }
