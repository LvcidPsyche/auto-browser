#!/usr/bin/env python3
"""Fail if the two copies of the stdio MCP bridge drift apart.

The bridge ships twice on purpose: ``client/auto_browser_client/mcp_bridge.py``
is what ``uvx auto-browser-mcp`` runs, and ``controller/app/mcp_stdio.py`` is
the same module inside the controller image (``python -m app.mcp_stdio``).
They are intentionally byte-identical, and nothing structural enforces that —
an edit applied to one silently strands the other, in a file that is on the
published PyPI surface. Same failure class as the version-string drift guarded
by ``check_version_parity.py``.

Exit 0 when the copies match, 1 when they differ (or either is missing).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

COPIES = (
    "client/auto_browser_client/mcp_bridge.py",
    "controller/app/mcp_stdio.py",
)


def main() -> int:
    contents: list[bytes] = []
    for relative in COPIES:
        path = REPO_ROOT / relative
        if not path.is_file():
            print(f"ERROR: missing bridge copy: {relative}", file=sys.stderr)
            return 1
        contents.append(path.read_bytes())

    if contents[0] == contents[1]:
        print(f"OK: {COPIES[0]} and {COPIES[1]} are byte-identical.")
        return 0

    print(
        "ERROR: the stdio MCP bridge copies have drifted apart.\n"
        f"  {COPIES[0]}\n"
        f"  {COPIES[1]}\n"
        "These are intentionally byte-identical; apply the same edit to both\n"
        "(or run: diff " + " ".join(COPIES) + " to see the drift).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
