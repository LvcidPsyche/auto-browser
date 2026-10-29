"""What an operator is shown of a governed tool call's arguments: credentials left out, by tool and path.

The approval's stand-in hashes every argument (McpToolGateway._governed_call_decision);
this is only what the operator reads, in the approval's reason. Approvals are
stored, listed and sent to webhooks, so a credential is left out of it: a
cookie's value, a storage value (shown by its length), and what a URL can carry
in its userinfo and credential query parameters (a CDP URL, a cookie's, a harness
run's start URL and its mock final page's). Nothing else is: every value under a
key named value, password, token or secret used to be hidden, at any depth,
which hid what decides what a call does (a harness run's start URL, auth profile
and conditions) and whatever an agent put under those names in a free-form dict.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from ..url_safety import browser_equivalent_url, host_as_sent

REDACTED = "<redacted>"
# What names a query parameter as a credential: one of these as a part of its name (split at punctuation
# and at camelCase: X-Amz-Signature, accessToken), or as the whole name (SAMLResponse). Its value is
# left out; every other parameter's is shown, since one such as a hosted browser's --proxy-server
# decides what the call does. A part, not a substring: monkey is no key. Hiding too much only hides a
# value from the operator's view.
CREDENTIAL_NAME_PARTS = frozenset(
    {
        "token",
        "key",
        "apikey",
        "secret",
        "password",
        "passwd",
        "pwd",
        "sig",
        "signature",
        "jwt",
        "session",
        "sessionid",
        "jsessionid",
        "phpsessid",
        "sid",
        "code",
        "auth",
        "authorization",
        "ticket",
        "credential",
        "credentials",
        "otp",
        "samlresponse",
    }
)
# Names that run credential words together, matched as the whole name: each is one part, so the
# parts above missed them (accesstoken, clientsecret).
CREDENTIAL_NAMES = frozenset(
    {
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "apitoken",
        "authtoken",
        "sessiontoken",
        "clientsecret",
        "apikey",
        "privatekey",
        "xapikey",
    }
)
# How many times a name is percent-decoded at most, until it reads the same: %2574oken is token.
NAME_DECODES = 2
# What separates query items: "&", and ";", which servers also split on.
_QUERY_SEPARATOR = re.compile(r"([&;])")
# Where a camelCase name splits: before an upper case letter after a lower case one or a digit
# (accessToken), and before the last capital of a run followed by a lower case letter (APIKey).
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NOT_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def redacted_url(url: Any) -> Any:
    """``url`` without the credentials it can carry: its userinfo, and the values of its credential parameters.

    A query value that is itself an http(s) URL (a return URL) loses its own the same way, one level deep.
    An internationalized host is written as a browser sends it (xn--), with the host as written beside it.
    """
    return _redacted_url(url, nested=True)


def _redacted_url(url: Any, *, nested: bool) -> Any:
    if not isinstance(url, str):
        return url
    # Read as a browser reads it (url_safety), so the host shown is the host used: urlsplit alone took
    # wss://x@evil.example\@bank.example/ for bank.example, behind the userinfo it left out.
    parts = urlsplit(browser_equivalent_url(url))
    _, at, hostport = parts.netloc.rpartition("@")
    # The URL itself; a URL in its query is shown as written, or decoded when something in it is left out.
    hostport, written = _as_sent(hostport) if nested else (hostport, None)
    netloc = f"{REDACTED}@{hostport}" if at else hostport
    pieces = _QUERY_SEPARATOR.split(parts.query) if parts.query else []
    # Every other piece is a separator, kept as written.
    query = "".join(
        piece if index % 2 else _redacted_query_item(piece, nested=nested) for index, piece in enumerate(pieces)
    )
    shown = urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    return f"{shown} (host as written: {written})" if written else shown


def _as_sent(hostport: str) -> tuple[str, str | None]:
    """``hostport`` with a non-ASCII host as a browser sends it, and that host as written; as it is, and None,
    for an ASCII host or one a browser could not map (the URL's validation refuses that one)."""
    if hostport.isascii():
        return hostport, None
    written, colon, port = hostport.partition(":")
    try:
        return f"{host_as_sent(written)}{colon}{port}", written
    except UnicodeError:
        return hostport, None


def _redacted_query_item(item: str, *, nested: bool) -> str:
    name, equals, value = item.partition("=")
    if not equals:
        return REDACTED if item else item  # an item without "=" may be a credential itself (?<token>)
    if _names_a_credential(name):
        return f"{name}={REDACTED}"
    inner = unquote_plus(value)
    # As a browser reads a URL (url_safety): past a leading space or control, and without tabs or newlines.
    if nested and browser_equivalent_url(inner).lower().startswith(("http://", "https://")):
        # A URL in the value, encoded or not: shown decoded only when something in it is left out.
        try:
            redacted = _redacted_url(inner, nested=False)
        except ValueError:
            # urllib reads no URL in it (https://[draft), and neither does a browser: text, as written.
            return item
        if redacted != inner:
            return f"{name}={redacted}"
    return item


def _names_a_credential(name: str) -> bool:
    # As decoded, so api%5Fkey is the api_key it stands for, and %2574oken (encoded twice) is token.
    decoded = name
    for _ in range(NAME_DECODES):
        once = unquote_plus(decoded)
        if once == decoded:
            break
        decoded = once
    whole = decoded.lower()
    parts = _NOT_ALPHANUMERIC.split(_CAMEL_BOUNDARY.sub(" ", decoded).lower())
    return (
        whole in CREDENTIAL_NAME_PARTS
        or whole in CREDENTIAL_NAMES
        or any(part in CREDENTIAL_NAME_PARTS for part in parts)
    )


def _hidden(_value: Any) -> str:
    return REDACTED


def _length_only(value: Any) -> str:
    return f"<redacted, {len(str(value))} characters>"


def _start_url_redacted(precondition: Any) -> Any:
    """A harness precondition with a start URL's credentials left out (redacted_url); any other kind as written."""
    if isinstance(precondition, dict) and precondition.get("kind") == "start_url":
        return {**precondition, "value": redacted_url(precondition.get("value"))}
    return precondition


# By tool: the path to each argument left out ("*" is every item of a list), and what is shown instead.
_REDACTIONS: dict[str, tuple[tuple[tuple[str, ...], Callable[[Any], Any]], ...]] = {
    "browser.set_cookies": ((("cookies", "*", "value"), _hidden), (("cookies", "*", "url"), redacted_url)),
    "browser.set_local_storage": ((("value",), _length_only),),
    "browser.cdp_attach": ((("cdp_url",), redacted_url),),
    "harness.start_convergence": (
        (("contract", "preconditions", "*"), _start_url_redacted),
        (("mock_final_observation", "url"), redacted_url),
    ),
}


def _redact_path(value: Any, path: tuple[str, ...], redact: Callable[[Any], Any]) -> Any:
    if not path:
        return redact(value)
    head, rest = path[0], path[1:]
    if head == "*":
        return [_redact_path(item, rest, redact) for item in value] if isinstance(value, list) else value
    if isinstance(value, dict) and head in value:
        return {**value, head: _redact_path(value[head], rest, redact)}
    return value


def preview_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """``arguments`` as the operator is shown them: the tool's credentials left out, nothing else changed."""
    shown = arguments
    for path, redact in _REDACTIONS.get(tool, ()):
        shown = _redact_path(shown, path, redact)
    return shown
