"""The observation's accessibility outline, built from Playwright's ARIA snapshot.

The outline came from ``page.accessibility.snapshot()``, which Playwright has
since removed; the pinned version has no ``page.accessibility``. Every outline
has read ``available: false`` since, so the documented accessibility tree was
missing from every preset, the OCR skip that needs it never applied (OCR ran on
every normal and rich observation), and the ``accessibility_focus_changed``
verification signal could not fire.

``page.aria_snapshot(mode="ai")`` is the replacement. It is YAML, one element
per line, e.g.::

    - generic [ref=e3]:
      - textbox "Email" [ref=e4]:
        - /placeholder: you@example.com
        - text: a@b.co
      - checkbox "Remember me" [checked] [active] [ref=e7]

It carries what a field holds — ``textbox "Password": hunter2`` for a filled
password field — so only roles, accessible names and states are kept. Values,
text runs and properties are dropped: the page text is in ``text_excerpt``, and
what the agent typed it already knows.
"""

from __future__ import annotations

import json
import re
from typing import Any

ACCESSIBILITY_NODE_LIMIT = 30

_LINE = re.compile(r"^(?P<indent> *)- (?P<body>.*)$")
_ENTRY = re.compile(r'^(?P<role>[a-z][a-z-]*)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<attrs>(?: \[[^\]]*\])*)$')
_ATTR = re.compile(r"\[(?P<key>[a-z-]+)(?:=(?P<value>[^\]]*))?\]")

# Unnamed elements worth a node: landmarks, containers and controls. Other
# unnamed roles (generic, paragraph, listitem, ...) are layout.
_STRUCTURAL_ROLES = frozenset(
    {
        "alert",
        "alertdialog",
        "article",
        "banner",
        "button",
        "checkbox",
        "combobox",
        "complementary",
        "contentinfo",
        "dialog",
        "form",
        "grid",
        "link",
        "list",
        "listbox",
        "main",
        "menu",
        "menubar",
        "navigation",
        "radio",
        "radiogroup",
        "region",
        "search",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "tab",
        "table",
        "tablist",
        "tabpanel",
        "textbox",
        "tree",
        "treegrid",
    }
)
# Never a node. Table cells are one node per datum and would crowd everything
# else out; the table's text is in text_excerpt, cell-separated.
_SKIPPED_ROLES = frozenset({"cell", "gridcell"})
# Not counted either: wrappers say nothing about the page.
_UNCOUNTED_ROLES = frozenset({"generic", "none", "presentation"})
_STATE_ATTRS = frozenset({"checked", "disabled", "expanded", "pressed", "selected", "level"})


def unavailable_outline(error: str | None = None) -> dict[str, Any]:
    outline: dict[str, Any] = {
        "available": False,
        "root_role": None,
        "root_name": None,
        "focused": None,
        "role_counts": {},
        "nodes": [],
    }
    if error:
        outline["error"] = error
    return outline


def outline_from_aria_snapshot(snapshot: str, *, limit: int = ACCESSIBILITY_NODE_LIMIT) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    role_counts: dict[str, int] = {}
    focused: dict[str, Any] | None = None
    # (indent, kept) for each open ancestor line; depth counts kept ancestors.
    ancestors: list[tuple[int, bool]] = []

    for line in snapshot.splitlines():
        match = _LINE.match(line)
        if match is None:
            continue
        indent = len(match["indent"])
        while ancestors and ancestors[-1][0] >= indent:
            ancestors.pop()
        body = match["body"]
        # A large page's snapshot runs to tens of thousands of lines, and past the
        # node limit only the role count and the focused node are still wanted.
        # Build the full node (decoded name, states) only for a line that can
        # still become one of those; for the rest the role and whether it has a
        # name are enough.
        if len(nodes) < limit or (focused is None and "[active]" in body):
            node = _parse_entry(body)
            role = node["role"] if node is not None else None
            has_name = node is not None and bool(node.get("name"))
        else:
            node = None
            role, has_name = _entry_role(body)
        kept = False
        if role is not None:
            if role not in _UNCOUNTED_ROLES:
                role_counts[role] = role_counts.get(role, 0) + 1
            kept = role not in _SKIPPED_ROLES and (has_name or role in _STRUCTURAL_ROLES)
        if kept and node is not None:
            appended = len(nodes) < limit
            if appended or (node.get("focused") and focused is None):
                node["depth"] = sum(1 for _, ancestor_kept in ancestors if ancestor_kept)
            if appended:
                nodes.append(node)
            if node.get("focused") and focused is None:
                focused = node
        ancestors.append((indent, kept))

    return {
        "available": True,
        "root_role": None,
        "root_name": None,
        "focused": focused,
        "role_counts": role_counts,
        "nodes": nodes,
    }


def _match_entry(body: str) -> re.Match[str] | None:
    entry = _entry_key(body)
    if entry is None or entry.startswith("/"):
        return None  # a property such as /url or /placeholder
    match = _ENTRY.match(entry)
    if match is None or match["role"] == "text":
        return None  # a run of text, or a line this parser does not know
    return match


def _entry_role(body: str) -> tuple[str | None, bool]:
    """The role of an element line and whether it has a non-empty name, without
    building its node. An escape sequence decodes to at least one character, so
    the raw name is empty exactly when the decoded one is."""
    match = _match_entry(body)
    if match is None:
        return None, False
    return match["role"], bool(match["name"])


def _parse_entry(body: str) -> dict[str, Any] | None:
    match = _match_entry(body)
    if match is None:
        return None
    node: dict[str, Any] = {"role": match["role"]}
    if match["name"] is not None:
        try:
            node["name"] = json.loads(f'"{match["name"]}"')
        except ValueError:
            node["name"] = match["name"]
    for attr in _ATTR.finditer(match["attrs"] or ""):
        key, value = attr["key"], attr["value"]
        if key == "active":
            node["focused"] = True
        elif key in _STATE_ATTRS:
            if value is None:
                node[key] = True
            elif key == "level" and value.isdigit():
                node[key] = int(value)
            else:
                node[key] = value  # e.g. checked=mixed
    return node


def _entry_key(body: str) -> str | None:
    """The element part of a snapshot line, without its trailing ``:`` or inline value."""
    if body.startswith("'"):
        # YAML single-quoted key: '' is an escaped quote.
        index = 1
        chars: list[str] = []
        while index < len(body):
            char = body[index]
            if char == "'":
                if body[index + 1 : index + 2] == "'":
                    chars.append("'")
                    index += 2
                    continue
                return "".join(chars)
            chars.append(char)
            index += 1
        return None
    # Unquoted keys never contain ": " (YAML would have quoted them), so the
    # first one separates the key from an inline value.
    key, _, _ = body.partition(": ")
    return key[:-1] if key.endswith(":") else key
