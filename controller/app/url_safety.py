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

import re
import unicodedata
from urllib.parse import urlsplit

from idna import uts46_remap

# WHATWG: strip leading and trailing C0 control-or-space, then remove every
# ASCII tab and newline from the input before parsing.
_C0_CONTROL_OR_SPACE = "".join(chr(code) for code in range(0x21))
_TAB_OR_NEWLINE = str.maketrans("", "", "\t\n\r")
_SPECIAL_SCHEMES = ("http:", "https:", "ws:", "wss:", "ftp:", "file:")
# A special scheme with an authority (file: has its own rules) and the run of slashes and backslashes after it.
_AUTHORITY_SLASHES = re.compile(r"^(https?:|wss?:|ftp:)[/\\]*", re.IGNORECASE)
# WHATWG reads a host whose last label is a number (decimal, or hex after 0x) as IPv4, in one to four
# decimal, octal (a leading 0) or hex parts; only the dotted quad of plain decimals is written as it reads.
_NUMBER_LABEL = re.compile(r"0[xX][0-9a-fA-F]*|[0-9]+")
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_CANONICAL_IPV4 = re.compile(rf"{_OCTET}(?:\.{_OCTET}){{3}}")


def browser_equivalent_url(url: str) -> str:
    """``url`` rewritten so urllib reads the same host a browser would load."""
    normalized = url.strip(_C0_CONTROL_OR_SPACE).translate(_TAB_OR_NEWLINE)
    if not normalized.lower().startswith(_SPECIAL_SCHEMES):
        return normalized
    # A browser skips any run of slashes and backslashes after a special scheme's colon, none included, and
    # reads the authority after it: https:u:p@h/ and https:////u:p@h/ are both u:p@h to it, none to urllib.
    normalized = _AUTHORITY_SLASHES.sub(r"\1//", normalized, count=1)
    # Backslash is a path separator for special schemes up to the query or
    # fragment; inside those it is a literal character in both parsers.
    cut = min((index for index in (normalized.find("?"), normalized.find("#")) if index != -1), default=-1)
    head, tail = (normalized, "") if cut == -1 else (normalized[:cut], normalized[cut:])
    return head.replace("\\", "/") + tail


def refuse_ambiguous_host(url: str, *, field_name: str) -> str:
    """``url`` as given, or ValueError when a browser could read another host in it than urllib does.

    For a URL the operator approves by reading it (a CDP URL, a cookie's url, a
    harness start URL): a backslash or a C0 control in it, or a host that
    browser_equivalent_url reads otherwise, would show the operator one host and
    connect to another; and a host the browser maps or decodes first (written
    in full-width or another form it maps to another name, percent-encoded, or
    IPv4 not as a dotted quad) is not the host shown either.
    """
    if "\\" in url or any(ord(char) < 0x20 for char in url):
        raise ValueError(
            f"{field_name} must not contain a backslash or a control character: a browser can read another host in it"
        )
    return refuse_another_navigation_host(url, field_name=field_name)


def refuse_another_navigation_host(url: str, *, field_name: str) -> str:
    """``url`` as given, or ValueError when a browser would load another host from it than urllib reads.

    refuse_ambiguous_host without its blanket rule, for a URL the agent
    navigates to (navigate, open_tab, a navigate action): a backslash or a
    control where it moves no host (in the query, in the path) is a page's to
    read, a search for C:\\Users among them. The host is read as a browser
    reads it (browser_equivalent_url: a backslash in the authority, a run of
    slashes after the scheme), and mapped, decoded or numeric hosts are
    refused as there. A control in the scheme or the authority is refused: a
    browser drops a tab or a newline there, and urllib too, so both read the
    same host, while the URL shown keeps it (a host punycoded with the tab in
    it, or a scheme no host is found after).
    """
    if any(ord(char) < 0x20 for char in _scheme_and_authority(url)):
        raise ValueError(
            f"{field_name} has a control character in its scheme or host, which a browser drops: it would show "
            "another host than the one a browser loads"
        )
    host = urlsplit(url).hostname
    if host != urlsplit(browser_equivalent_url(url)).hostname:
        raise ValueError(f"{field_name} names another host to a browser than it shows")
    # As written: urlsplit lower-cases the host, and a browser maps what is written.
    written = urlsplit(url).netloc.rpartition("@")[2].partition(":")[0]
    if not written.isascii():
        try:
            mapped = browser_host(written)
            # urllib's host, lower-cased its own way, must be the same host to a browser: a capital sigma that
            # ends a label is a final small sigma to str.lower, a small sigma to UTS 46.
            urllib_mapped = host if not host or host.isascii() else browser_host(host)
        except UnicodeError as error:
            raise ValueError(f"{field_name} writes its host {written!r} with what a browser refuses: {error}") from None
        # Beyond case folding and composition (Cherokee capitals, a name written decomposed; casefold also
        # maps the fi ligature, the long s and the micro sign as UTS 46 does, so those are accepted): full-width
        # forms, segmented digits, a dropped ignorable.
        if _folded(mapped) != _folded(written):
            raise ValueError(
                f"{field_name} writes its host {written!r} in a form a browser reads as another host, {mapped!r}"
            )
        if urllib_mapped != mapped:
            raise ValueError(
                f"{field_name} writes its host {written!r} in a form a browser reads as another host than urllib"
                f" does ({mapped!r}, not {urllib_mapped!r})"
            )
    if host and _decoded_by_a_browser(host):
        raise ValueError(f"{field_name} writes its host {host!r} in a form a browser reads as another host")
    return url


def _scheme_and_authority(url: str) -> str:
    """What of ``url`` comes before its path, query or fragment: its scheme, the slashes after it, its authority.

    As a browser reads them for a special scheme: any run of slashes and backslashes after the colon, and the
    authority up to the next slash, backslash, ? or #. With no colon, all of it.
    """
    colon = url.find(":")
    if colon == -1:
        return url
    authority = url[colon + 1 :].lstrip("/\\")
    end = next((index for index, char in enumerate(authority) if char in "/\\?#"), len(authority))
    return url[: len(url) - len(authority) + end]


def _folded(text: str) -> str:
    """``text`` composed (NFC) and case-folded: what is left of a host once case and composition are set aside."""
    return unicodedata.normalize("NFC", text).casefold()


def browser_host(host: str) -> str:
    """``host``, as written in a URL, as a browser maps it before it connects; IDNAError when it cannot.

    WHATWG's host parser maps a domain through UTS 46's table (nontransitional,
    without STD3 rules), as idna.uts46_remap does: full-width digits and dots
    and other compatibility forms fold to ASCII, an ignorable code point
    (U+200B, U+1BCA0) is dropped, letters are lower-cased, and a code point it
    disallows is an error. Python's idna codec does none of it for what Unicode
    3.2 did not have (it read Unicode 13's segmented 127.1 as a name) and writes
    straße as strasse. An ASCII name is only lower-cased, so the callers pass
    only a host that is not ASCII.
    """
    return uts46_remap(host, std3_rules=False, transitional=False)


def host_as_sent(host: str) -> str:
    """``host``, as written in a URL, as a browser sends it: mapped (browser_host), each non-ASCII label as xn--.

    Punycode per label, as WHATWG's domain to ASCII writes it.
    """
    return ".".join(
        label if label.isascii() else "xn--" + label.encode("punycode").decode("ascii")
        for label in browser_host(host).split(".")
    )


def _decoded_by_a_browser(host: str) -> bool:
    """Whether a browser connects to ``host`` under another name: percent-encoded, or IPv4 not as a dotted quad.

    %62ank.example is bank.example to it, and 2130706433, 0177.0.0.01,
    0x7f000001 and 127.1 are 127.0.0.1. An IPv6 literal is left alone.
    """
    if "%" in host:
        return True
    if ":" in host:
        return False
    labels = host.split(".")
    last = labels[-1] or (labels[-2] if len(labels) > 1 else "")
    return bool(_NUMBER_LABEL.fullmatch(last)) and not _CANONICAL_IPV4.fullmatch(host)
