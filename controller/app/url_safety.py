"""Read a URL's host the way the browser will, before deciding whether to load it.

Allowlist checks parse URLs with Python's urllib, but Chromium parses them per
the WHATWG URL standard, and the two disagree. The one that matters: for
http(s) — the "special" schemes — a browser treats a backslash in the
authority or path as a forward slash; urllib keeps it as an ordinary character.
So for ``http://evil.com\\@example.com/`` urllib reports the host
``example.com`` (everything before the last "@" is userinfo to it), while
Chromium loads ``evil.com`` with path ``/@example.com/``. An allowlist that
trusted urllib's answer let a caller name any host by appending
``\\@<allowlisted-host>``.

``browser_equivalent_url`` rewrites a URL into the form both parsers agree on,
and allowlist checks parse that instead of the raw input.
"""

from __future__ import annotations

# WHATWG: strip leading and trailing C0 control-or-space, then remove every
# ASCII tab and newline from the input before parsing.
_C0_CONTROL_OR_SPACE = "".join(chr(code) for code in range(0x21))
_TAB_OR_NEWLINE = str.maketrans("", "", "\t\n\r")
_SPECIAL_SCHEMES = ("http:", "https:", "ws:", "wss:", "ftp:", "file:")


def browser_equivalent_url(url: str) -> str:
    """``url`` rewritten so urllib reads the same host a browser would load."""
    normalized = url.strip(_C0_CONTROL_OR_SPACE).translate(_TAB_OR_NEWLINE)
    if not normalized.lower().startswith(_SPECIAL_SCHEMES):
        return normalized
    # Backslash is a path separator for special schemes up to the query or
    # fragment; inside those it is a literal character in both parsers.
    cut = min((index for index in (normalized.find("?"), normalized.find("#")) if index != -1), default=-1)
    head, tail = (normalized, "") if cut == -1 else (normalized[:cut], normalized[cut:])
    return head.replace("\\", "/") + tail
