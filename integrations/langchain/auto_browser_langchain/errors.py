from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import unquote, unquote_plus

import httpx

# httpx's words for each class of status raise_for_status refuses.
_STATUS_KINDS = {1: "Informational response", 3: "Redirect response", 4: "Client error", 5: "Server error"}

# The MCP bridges' redaction (auto_browser_client.mcp_bridge._redacted, which the client SDK shares); this package
# depends on httpx only, so it carries a copy. A userinfo-shaped run in a text: characters other than whitespace,
# quotes, / and @, ending in @, starting where a run starts (W13-1, W14-1). A query value this long or longer is
# redacted on its own too (R5C-20).
_USERINFO_RUN = re.compile(r"""(?:(?<![^\s'"@])|(?<=//))[^\s'"/@]*@""")
_SHORTEST_REDACTED_QUERY_VALUE = 8


@contextmanager
def errors_shown(base_url: str, token: str | None) -> Iterator[None]:
    """Raise an httpx error from inside again without the base URL's secrets or ``token`` in its text, its type and
    attributes kept (review of c3, R5C-20): httpx's error for an answer it cannot read quotes what the server sent,
    which can echo the request line (the base URL's query) or a header (the token), and a status error's reason
    phrase is the server's text too."""
    try:
        yield
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        text = str(error)
        shown = _redacted(text, _as_sent(base_url), token)
        if shown == text:
            raise
        copy = type(error).__new__(type(error))
        copy.__dict__.update(error.__dict__)
        copy.args = (shown,)
        raise copy.with_traceback(error.__traceback__) from None


def _as_sent(base_url: str) -> tuple[str, ...]:
    """``base_url`` as configured and as httpx sends it, whose secrets an error leaves out (review of c3's fixes):
    httpx percent-encodes a space or a character outside ASCII in the userinfo and the query, and writes the query
    again as the params it sends (a space as ``+``), so an echo of the request quotes a form the configured text does
    not hold. Only the first when httpx cannot read it (a lone surrogate fails its encoding), as this runs while
    another error is shown."""
    try:
        url = httpx.URL(base_url)
        return (base_url, str(url), str(url.copy_with(query=None).copy_merge_params(url.params)))
    except (httpx.InvalidURL, UnicodeError):
        return (base_url,)


def _redacted(text: str, url: str | tuple[str, ...], token: str | None) -> str:
    """``text`` with ``url``'s secrets and ``token`` replaced with ``[redacted]`` in one pass, longest first, and any
    other userinfo run dropped, as the MCP bridges' answers show a text. ``url`` can be several forms of one URL."""
    secrets = sorted(_secrets_of(url, token), key=lambda secret: (-len(secret), secret))
    if secrets:
        text = re.compile("|".join(map(re.escape, secrets))).sub("[redacted]", text)
    return _USERINFO_RUN.sub("", text)


def _secrets_of(url: str | tuple[str, ...], token: str | None) -> set[str]:
    """``url``'s userinfo, password, query, fragment and each query value of _SHORTEST_REDACTED_QUERY_VALUE or more
    characters, as written and percent-decoded (a query's ``+`` as a space too), and ``token``, each also as
    ``repr()`` and ``json.dumps()`` write it, and the Basic credentials httpx sends for the userinfo; for each form of
    the URL when given several. Cut as a string, so a malformed URL is handled too."""
    if not isinstance(url, str):
        return set().union(*(_secrets_of(form, token) for form in url))
    url = url.strip()
    _, slashes, rest = url.partition("//")
    if not slashes:
        rest = url
    end = min((index for index in (rest.find(delimiter) for delimiter in "/?#") if index != -1), default=len(rest))
    userinfo = rest[: max(rest.rfind("@", 0, end), 0)]
    query = url.partition("?")[2].partition("#")[0]
    values = [item.partition("=")[2] if "=" in item else item for item in query.split("&")]
    in_query = {query, *(value for value in values if len(value) >= _SHORTEST_REDACTED_QUERY_VALUE)}
    written = {userinfo, userinfo.partition(":")[2], url.partition("#")[2], *in_query}
    forms = {*written, *map(unquote, written), *map(unquote_plus, in_query), token or "", _basic_credentials(userinfo)}
    quoted = {
        form[1:-1]
        for value in forms
        for form in (repr(value), json.dumps(value), json.dumps(value, ensure_ascii=False))
    }
    return {secret for secret in forms | quoted if secret}


def _basic_credentials(userinfo: str) -> str:
    """What httpx sends for ``userinfo`` after ``Authorization: Basic``: the base64 of its user name and password, each
    percent-decoded, joined by a colon, as UTF-8; "" when both are empty, as httpx then sends none. An echo of that
    header showed the URL's credentials (review of c3's fixes). A lone surrogate, which httpx cannot encode (so it
    sends no header), is encoded anyway rather than failing the redaction."""
    user, _, password = userinfo.partition(":")
    user, password = unquote(user), unquote(password)
    if not user and not password:
        return ""
    return base64.b64encode(f"{user}:{password}".encode("utf-8", "surrogatepass")).decode("ascii")


def raise_for_status(response: httpx.Response) -> None:
    """Raise httpx.HTTPStatusError for a response that is not 2xx, as ``response.raise_for_status()`` does and in its
    words, but naming the request URL without its userinfo, query or fragment (R5C-20).

    httpx's message names the URL whole, the password, a token in the query and the fragment included, and an agent
    shown the error (LangGraph's ToolNode hands it the exception's text) read them.
    """
    if response.is_success:
        return
    kind = _STATUS_KINDS.get(response.status_code // 100, "Invalid status code")
    url = response.request.url.copy_with(userinfo=b"", query=None, fragment=None)
    raise httpx.HTTPStatusError(
        f"{kind} '{response.status_code} {response.reason_phrase}' for url '{url}'\n"
        f"For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/{response.status_code}",
        request=response.request,
        response=response,
    )


class AutoBrowserToolError(RuntimeError):
    """A tool call the controller answered with ``isError: true``.

    /mcp/tools/call reports a failed tool as HTTP 200, so ``raise_for_status``
    never sees it. ``payload`` is the controller's ``structuredContent`` when it
    sent an object, e.g. ``{"error": ..., "code": "selector_not_found",
    "retryable": False}`` or ``{"error": ..., "code": "unknown_session"}``; it is
    None for a bare text error such as "Tool execution failed". Check ``code``
    to tell a missing session from a bad selector.
    """

    def __init__(self, tool: str, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(f"{tool} failed: {message}")
        self.tool = tool
        self.message = message
        self.payload = payload

    @property
    def code(self) -> str | None:
        value = (self.payload or {}).get("code")
        return value if isinstance(value, str) else None

    @property
    def retryable(self) -> bool | None:
        value = (self.payload or {}).get("retryable")
        return value if isinstance(value, bool) else None
