"""Stdio <-> HTTP bridge for Auto Browser's MCP endpoint.

Single-file and stdlib-only by design. This module ships in two places:
as ``auto_browser_client.mcp_bridge`` (the PyPI ``auto-browser-mcp`` console
script) and as ``app.mcp_stdio`` inside the controller image (Glama/Docker
entrypoint). A guard test asserts the two copies stay byte-identical, so any
edit here must be mirrored to the other copy.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.request
from dataclasses import dataclass
from http.client import HTTPConnection, InvalidURL
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, unquote_plus, unwrap, urlsplit
from urllib.request import (
    HTTPDefaultErrorHandler,
    HTTPErrorProcessor,
    HTTPHandler,
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    UnknownHandler,
)

# MCP spec header names, kept as local constants so the bridge has no
# controller dependency. The controller test suite asserts these match
# app.mcp_transport's values.
MCP_SESSION_HEADER = "MCP-Session-Id"
MCP_PROTOCOL_HEADER = "MCP-Protocol-Version"

# The HTTP transport pushes notifications/resources/updated over its event
# stream. This bridge only POSTs and never opens that stream, so a stdio client
# could subscribe and then never hear anything. Say so up front instead.
_SUBSCRIPTION_METHODS = frozenset({"resources/subscribe", "resources/unsubscribe"})


class _NoRedirects(HTTPRedirectHandler):
    """Refuse every redirect, so the bridge's headers never reach another URL.

    urllib re-sent a POST answered 301, 302 or 303 as a GET with the same
    headers, Authorization included, to wherever Location pointed. MCP never
    redirects; the 3xx reaches the client as an HTTP error instead.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener(proxies: dict[str, str] | None = None) -> OpenerDirector:
    """An opener for http:// and https:// only that never follows a redirect, using the http and https entries of
    ``proxies`` as ProxyHandler takes them (None: the environment's, read now).

    urllib's build_opener adds its file:, data: and ftp: handlers too, which answer such a URL (or a request an
    environment proxy of that scheme takes) from a local file, the URL itself or an FTP server, as if a controller had;
    here any other scheme meets UnknownHandler's URLError (R5C-28). ProxyHandler turns a request of any scheme it
    holds a proxy for into an http one to that proxy, before UnknownHandler is reached: with ftp_proxy set, an ftp://
    URL went to that proxy, token included, and its answer was taken as the controller's (c4).
    """
    proxies = urllib.request.getproxies() if proxies is None else proxies
    kept = {scheme: proxy for scheme, proxy in proxies.items() if scheme in ("http", "https")}
    handlers = [ProxyHandler(kept), UnknownHandler(), HTTPHandler(), HTTPDefaultErrorHandler(), _NoRedirects()]
    https = getattr(urllib.request, "HTTPSHandler", None)  # absent from a Python built without ssl
    if https is not None:
        handlers.append(https())
    opener = OpenerDirector()
    for handler in (*handlers, HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


_DIRECT = _opener({})

# A userinfo-shaped run in an error's text: characters other than whitespace, quotes, / and @, ending in @ (W13-1). It
# starts at the text's start, or after whitespace, a quote, an @ or the // before an authority, never after a path's
# single / (R5C-20: /a@b/mcp is a path); so a match starts only where a run starts, and the scan is linear (W14-1).
_USERINFO_RUN = re.compile(r"""(?:(?<![^\s'"@])|(?<=//))[^\s'"/@]*@""")

# A query value this long or longer is redacted on its own too, as a server's detail can quote one without its key; a
# shorter one (a page number, a flag) would cut words out of every answer (R5C-20).
_SHORTEST_REDACTED_QUERY_VALUE = 8
# How deep _redacted_value walks a server's JSON. JSON parses deeper than Python's recursion limit lets a walk go, and
# the walk's RecursionError ended the bridge with no answer (review of c4); a value deeper than this is replaced.
_REDACTED_DEPTH_MAX = 100
_TOO_DEEP = "[redacted: nested too deep]"


class InvalidControllerURL(InvalidURL):
    """The controller URL cannot be read, or reads as a local host it would not connect to; nothing was sent.

    Its text is a fixed reason in the bridges' own words, never an exception's text, which can quote the URL (W14-1).
    """


def _without_userinfo(text: str) -> str:
    """``text`` with every userinfo-shaped run removed, so an error the agent sees carries no ``user:password@``."""
    return _USERINFO_RUN.sub("", text)


def _split_userinfo(url: str) -> tuple[str, str, str]:
    """``url`` as (everything before the userinfo, the userinfo, everything after its ``@``), cut as a string.

    The userinfo runs from ``//``, or the start when there is none, up to the last ``@`` before the first ``/``,
    ``?`` or ``#``; it is empty when there is no such ``@``.
    """
    head, slashes, rest = url.partition("//")
    if not slashes:
        head, rest = "", url
    end = min((index for index in (rest.find(delimiter) for delimiter in "/?#") if index != -1), default=len(rest))
    at = rest.rfind("@", 0, end)
    return head + slashes, rest[: max(at, 0)], rest[at + 1 :]


def _display_url(url: str) -> str:
    """``url`` without its userinfo, query or fragment, cut as a string so a malformed URL is handled too (W13-1).

    The URL is read first as urllib's Request reads it, without surrounding whitespace or a ``<URL:...>`` wrapper
    (W15-6).
    """
    before, _, after = _split_userinfo(unwrap(url))
    return _without_userinfo((before + after).split("#", 1)[0].split("?", 1)[0])


def _redacted(text: str, url: str | tuple[str, ...], token: str | None = None) -> str:
    """``text`` with ``url``'s secrets and ``token`` replaced with ``[redacted]``, and any userinfo run dropped (W14-1).

    The secrets (``_secrets_of``) are replaced in one pass, longest first (R5C-20), so a short one is never replaced
    again inside a ``[redacted]`` already written; then ``_without_userinfo`` drops any other ``user:password@``.
    ``url`` can be several forms of one URL, whose secrets all count (the client SDK gives its base URL as configured
    and as httpx sends it).
    """
    secrets = sorted(_secrets_of(url, token), key=lambda secret: (-len(secret), secret))
    if secrets:
        text = re.compile("|".join(map(re.escape, secrets))).sub("[redacted]", text)
    return _without_userinfo(text)


def _secrets_of(url: str | tuple[str, ...], token: str | None) -> set[str]:
    """What an answer must not show of ``url`` and ``token``, in each form a text can quote it in.

    The URL is read first as urllib's Request reads it (W15-6): a CR left on it by a CRLF .env file, which urllib
    strips, would otherwise end the query and match no text. Its userinfo, password, query and fragment, and each
    query value of _SHORTEST_REDACTED_QUERY_VALUE or more characters (R5C-20), are found with string operations, so a
    malformed URL is handled, and count as written and percent-decoded (a query's ``+`` as a space too); each of
    those, and ``token``, counts as ``repr()`` and ``json.dumps()`` write it too (R5C-20). The userinfo counts as
    the Basic credentials httpx sends for it too (``_basic_credentials``). Several forms of one URL count each: httpx
    percent-encodes a space or a character outside ASCII in the query it sends, which a server then quotes (review of
    c3's fixes).
    """
    if not isinstance(url, str):
        return set().union(*(_secrets_of(form, token) for form in url))
    url = unwrap(url)
    userinfo = _split_userinfo(url)[1]
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
    percent-decoded, joined by a colon, as UTF-8; "" when both are empty, as httpx then sends none.

    The client SDK shares this redaction, and an echo of that header showed the URL's credentials (review of c3's
    fixes); the bridges send no Basic header, so for them it only redacts more. A lone surrogate, which httpx cannot
    encode (so it sends no header), is encoded anyway rather than failing the redaction.
    """
    user, _, password = userinfo.partition(":")
    user, password = unquote(user), unquote(password)
    if not user and not password:
        return ""
    return base64.b64encode(f"{user}:{password}".encode("utf-8", "surrogatepass")).decode("ascii")


def _redacted_value(value: Any, url: str | tuple[str, ...], token: str | None = None, *, _depth: int = 0) -> Any:
    """``value``, a server's error detail as JSON parsed it, with each string in it (keys too) through ``_redacted``
    and each number that is one of the secrets replaced, its shape kept (R5C-20). A list or an object more than
    _REDACTED_DEPTH_MAX deep is replaced by _TOO_DEEP, not walked."""
    if isinstance(value, str):
        return _redacted(value, url, token)
    if isinstance(value, (dict, list)) and _depth >= _REDACTED_DEPTH_MAX:
        return _TOO_DEEP
    if isinstance(value, dict):
        return {
            _redacted(str(key), url, token): _redacted_value(item, url, token, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redacted_value(item, url, token, _depth=_depth + 1) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if _redacted(str(value), url, token) == str(value) else "[redacted]"
    return value


def _is_loopback_host(host: str | None) -> bool:
    """Whether a request to this host stays on this machine, so it goes past any environment proxy.

    One rule for every Python sender of the token: localhost, also with one trailing dot, or an address that is
    loopback or unspecified (0.0.0.0 and ::, which a server's startup line prints and which reach this host on Linux
    and macOS), an IPv4-mapped IPv6 address counting as the IPv4 address it maps. The address is the IP literal, or
    what the platform's numeric parser reads the host as (W9-5: 127.2, 2130706434 and 0x7f000002 are 127.0.0.2 to
    the socket layer), never a DNS answer. A *.localhost name is not local here. The name compared is the one the
    socket layer resolves (W12-1): the host encoded with the ``idna`` codec, as getaddrinfo encodes it, and
    lowercased, so local<U+00AD>host and l<U+00BA>calhost are localhost; a host the codec refuses is compared as
    given.
    """
    host = host or ""
    try:
        name = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        name = host
    if name.removesuffix(".") == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        numeric = _numeric_address(host)
        if numeric is None:
            return False
        address = numeric
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def _numeric_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address getaddrinfo reads ``host`` as with AI_NUMERICHOST only, which never queries DNS; None otherwise."""
    if not host:
        return None
    try:
        infos = socket.getaddrinfo(host, None, flags=socket.AI_NUMERICHOST)
        return ipaddress.ip_address(infos[0][4][0])
    except (OSError, UnicodeError, ValueError, IndexError):
        return None


def _not_for_a_request_line(text: str) -> bool:
    """Whether ``text`` holds a space, a control character (U+0000 to U+0020 or U+007F) or a character outside ASCII,
    none of which http.client sends in a request line."""
    return any(not " " < character < "\x7f" for character in text)


def _goes_direct(request: Request) -> bool:
    """Whether the request goes past any environment proxy: the host http.client connects to is local (W13-2).

    One fail-closed rule over five readings of the host. Direct only when (1) the host http.client connects to,
    which reads an unbracketed ::1:8000 as ::1, is local: going direct sends the request to that host and no other.
    When it is not, but another reading is, the request is refused with InvalidControllerURL: (2) the host of
    urllib's percent-decoded ``request.host`` read as a URL authority (W9-5: %31%32%37.0.0.2 is 127.0.0.2), which
    drops a userinfo http.client keeps in its host; (3) the host of the URL as written, which a proxy reads, so a
    percent-encoded / ? or # in a userinfo (x%2f@127.0.0.1) cannot hide a loopback host; (4) urllib's decoded
    ``request.host`` as a whole after its last @ (W12-2), so an unbracketed ::1 or :: without a port, which
    http.client reads as host ":" and urlsplit as no host, is found local, also behind a userinfo (user@::1); (5) the
    host http.client reads from that decoded host after its last @ (W14-4), so a userinfo before an unbracketed IPv6
    loopback with a port (user@::1:8000, whose connect host is user@::1) cannot hide it. Such a
    host would be connected to as another name (127.0.0.2#.bad, x/@127.0.0.1, ":") that the resolver is asked for,
    and a proxy would get the token, so it goes nowhere. A host no reading finds local uses the proxy.
    Constructing the HTTPConnection parses and does not connect; its InvalidURL (a non-numeric port), a port it
    reads outside 0 to 65535 (W15-4), and a reading urlsplit cannot parse (%5b127.0.0.1%5d, x%5b@127.0.0.1) and
    its ValueError (W12-3), raise InvalidControllerURL (W13-1), so a malformed authority is refused before any
    opener is chosen and the request is sent nowhere. So, before the readings, does a ``request.selector`` (the
    path and query) holding a character a request line cannot carry, which http.client refuses (a space or a
    control character, U+0000 to U+0020 or U+007F) or cannot encode (one outside ASCII) only once a connection is
    chosen, and a fragment holding one, which urllib puts into a proxied request line (R5C-28). Each reason is the
    bridges' own, never the parse error's text, which can quote the URL (W14-1). A host-less request
    (``request.host`` None), which ``urlopen`` refuses before asking, is not direct (W10-14).
    """
    if request.host is None:
        return False
    if _not_for_a_request_line(request.selector):
        raise InvalidControllerURL(
            "its path or query holds a space, a control character or a character outside ASCII; percent-encode it"
        )
    if _not_for_a_request_line(request.fragment or ""):
        raise InvalidControllerURL(
            "its fragment holds a space, a control character or a character outside ASCII; percent-encode it or "
            "leave the fragment out"
        )
    try:
        connection = HTTPConnection(request.host)
    except InvalidURL as exc:
        raise InvalidControllerURL(
            "http.client cannot read its host and port (a port that is not a number, a userinfo, or a space or "
            "control character)"
        ) from exc
    # http.client reads :99999, :65536 and :-1 as those numbers, and the socket layer wraps them (on Windows,
    # getaddrinfo connects them to 34463, 0 and 65535), so the token could reach another port than the one written.
    if not 0 <= connection.port <= 65535:
        raise InvalidControllerURL("its port is outside 0 to 65535")
    connected = connection.host
    try:
        others = (
            urlsplit("//" + request.host).hostname,
            urlsplit(request.full_url).hostname,
            request.host.rpartition("@")[2],
            HTTPConnection(request.host.rpartition("@")[2]).host,
        )
    except (InvalidURL, ValueError) as exc:
        raise InvalidControllerURL("a reading cannot parse its host (for example, a malformed bracket)") from exc
    # Codex R01: urllib reads http://:8000/mcp's host as ":8000" and no reading finds a local host in an empty
    # one, so the request went to the environment proxy with the token. The fifth reading is the host http.client
    # reads after the last @.
    if not others[-1]:
        raise InvalidControllerURL("its host is empty")
    if _is_loopback_host(connected):
        return True
    if any(_is_loopback_host(host) for host in others):
        # Never the host or its userinfo: the message reaches the agent through the bridges' answer.
        raise InvalidControllerURL(
            "the host reads as a local host one way and as another host the way it would be connected to "
            "(a userinfo, an IPv6 address without brackets, or an encoded delimiter); name a local controller "
            "plainly, as http://127.0.0.1:PORT or http://[::1]:PORT"
        )
    return False


# Module-level, as urllib's own urlopen was, so tests can patch it.
def urlopen(request: Request, *args: Any, **kwargs: Any) -> Any:
    """Open the request without following redirects, past any environment proxy when its host is loopback.

    urllib skips a proxy for localhost only when no_proxy names it, so with http_proxy set the bridge's
    Authorization header for a local controller went to the proxy. Any other host still uses the proxy the
    environment names, read for each request. ``_goes_direct`` decides, from the host http.client connects to, and
    refuses a host another reading finds local.

    A request that is not http:// or https:// with a host is refused first, so nothing is sent (W15-3, c4): urllib
    reads a scheme-less userinfo's user name as the scheme (sentinel:x@host/mcp), answers a data: or file: URL from
    the URL or the local file as if a controller had, sends a URL of any scheme an environment proxy names (ftp_proxy)
    to that proxy, and leaves a URL without a host to fail later. Its type is lowercased; its host is "" for
    http:///mcp and None for http:mcp.

    ``_goes_direct`` checks the path, query and fragment (R5C-28); a host or userinfo http.client cannot send is
    refused once it fails to encode, before any byte goes out (review of c3): urllib puts the URL whole into the
    request line an environment proxy gets, which goes as ASCII, and the host it decoded, userinfo included, into the
    Host header, which goes as latin-1, and the UnicodeEncodeError named the character and its position.
    """
    if request.type not in ("http", "https") or not request.host:
        raise InvalidControllerURL("it is not an http:// or https:// URL with a host")
    opener = _DIRECT if _goes_direct(request) else _opener()
    authority = (request.host or "", urlsplit(request.full_url).netloc)  # as urllib decoded it, and as written
    try:
        return opener.open(request, *args, **kwargs)
    except UnicodeEncodeError as exc:
        if not any(part and part in exc.object for part in authority):
            raise
        raise InvalidControllerURL(
            "its host or userinfo holds a character http.client cannot send (outside ASCII through a proxy, outside "
            "latin-1 in the Host header); write the host in its IDNA (xn--) form and leave any userinfo out"
        ) from exc


# Where a token given as an argument comes from, as the reason it went unsent names it (W15-7).
_ARGUMENT_SOURCE = "--bearer-token or AUTO_BROWSER_BEARER_TOKEN"


def _unsent_because(source: str, token: str) -> str | None:
    """Why ``token`` from ``source`` cannot go in a header, as fixed text that names the source and never the token;
    None when it can (W15-7, R5C-30).

    http.client refuses a line break with a ValueError that quotes the header whole, and encodes a header as latin-1,
    whose UnicodeEncodeError names a character outside it and its position: an answer would show the agent either.
    A token inside latin-1 is sent, ASCII or not: the controller reads the header as latin-1.
    """
    if "\r" in token or "\n" in token:
        held = "a line break"
    else:
        try:
            token.encode("latin-1")
            return None
        except UnicodeEncodeError:
            held = "a character outside latin-1"
    return f"{source} holds {held}, which an HTTP header cannot carry, so no token was sent"


@dataclass
class HttpMcpResponse:
    status_code: int
    headers: dict[str, str]
    body: dict[str, Any] | None


class HttpMcpClient:
    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str | None = None,
        timeout_seconds: float = 60.0,
        operator_id: str | None = None,
        token_file: str | None = None,
    ):
        self.base_url = base_url
        self.bearer_token = bearer_token.strip() if bearer_token else None
        self.timeout_seconds = timeout_seconds
        self.operator_id = operator_id
        self.token_file = os.path.expanduser(token_file) if token_file else None
        # Why the last request went without a token, from token_file or bearer_token; added to a 401's message.
        self.token_file_problem: str | None = None
        # The token the last request sent, or would not send: what an answer shows never holds it (R5C-20).
        self.last_token: str | None = None

    def _bearer(self) -> str | None:
        """The token to send: bearer_token when set, else the one in token_file, read now.

        Read on every request, because a stdio client starts the bridge before the
        controller has written the file, and a regenerated token must not need a
        restart. A missing or unreadable file sends no header, so a controller
        running without a token still works.

        A token holding a line break (W15-7) or a character outside latin-1 (R5C-30), from either source, sends no
        header either: http.client's errors for those quote the header or name the character, which an answer would
        show the agent. ``token_file_problem`` says why the last request went without a token, for bearer_token as
        well as for token_file.
        """
        if self.bearer_token:
            self.last_token = self.bearer_token
            self.token_file_problem = _unsent_because(_ARGUMENT_SOURCE, self.bearer_token)
            return None if self.token_file_problem else self.bearer_token
        if not self.token_file:
            return None
        try:
            with open(self.token_file, encoding="utf-8") as handle:
                token = handle.read().strip()
        except UnicodeDecodeError:
            # Not the decode error's text, which names one of the token's bytes and its position, as R5C-30's encode
            # error named a character (review of c3).
            self.token_file_problem = f"the token file {self.token_file} is not UTF-8 text, so no token was sent"
            return None
        except OSError as exc:
            self.token_file_problem = f"could not read the token file {self.token_file}: {exc}"
            return None
        self.last_token = token or None
        self.token_file_problem = _unsent_because(f"the token file {self.token_file}", token)
        if self.token_file_problem:
            return None
        self.token_file_problem = None if token else f"the token file {self.token_file} is empty"
        return token or None

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        bearer = self._bearer()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if self.operator_id:
            headers["X-Operator-Id"] = self.operator_id
        return headers

    def post_json(
        self,
        payload: dict[str, Any],
        *,
        session_id: str | None = None,
        protocol_version: str | None = None,
    ) -> HttpMcpResponse:
        headers = {"Content-Type": "application/json"}
        if session_id:
            headers[MCP_SESSION_HEADER] = session_id
        if protocol_version:
            headers[MCP_PROTOCOL_HEADER] = protocol_version
        headers.update(self._auth_headers())
        return self._request("POST", headers=headers, body=payload)

    def delete_session(self, *, session_id: str | None) -> None:
        if not session_id:
            return
        try:
            headers = {MCP_SESSION_HEADER: session_id, **self._auth_headers()}
            self._request("DELETE", headers=headers, body=None)
        except Exception:
            return

    def _request(self, method: str, *, headers: dict[str, str], body: dict[str, Any] | None) -> HttpMcpResponse:
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            request = Request(self.base_url, data=payload, headers=headers, method=method)
        except ValueError as exc:
            # urllib's text quotes the URL, query and fragment included (W14-1).
            raise InvalidControllerURL(
                "urllib cannot read it as an http:// or https:// URL (for example, no scheme, a malformed bracket or "
                "a character it refuses)"
            ) from exc
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                return HttpMcpResponse(
                    status_code=response.status,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=self._decode_json(raw),
                )
        except HTTPError as exc:
            return HttpMcpResponse(
                status_code=exc.code,
                headers={k.lower(): v for k, v in exc.headers.items()},
                body=self._decode_json(exc.read()),
            )

    @staticmethod
    def _decode_json(raw: bytes) -> dict[str, Any] | None:
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            # A proxy's HTML error page, say, or a body nested too deep for json to parse. Reported with its status
            # below.
            return None


class StdioMcpBridge:
    def __init__(self, *, client: HttpMcpClient, stderr: TextIO | None = None):
        self.client = client
        self.stderr = stderr or sys.stderr
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        # The client's initialize request, replayed if the controller forgets
        # the session (restart without a session store, or eviction).
        self._initialize_payload: dict[str, Any] | None = None
        self._initialized_sent = False

    def run(self, *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
        input_stream = stdin or sys.stdin
        output_stream = stdout or sys.stdout
        try:
            for raw_line in input_stream:
                line = raw_line.strip()
                if not line:
                    continue
                response = self._handle_line(line)
                if response is not None:
                    output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
                    output_stream.flush()
        finally:
            self.client.delete_session(session_id=self.session_id)
        return 0

    def _handle_line(self, line: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return self._jsonrpc_error(None, -32700, "Invalid JSON payload")

        if isinstance(payload, list):
            return self._jsonrpc_error(None, -32600, "JSON-RPC batches are not supported")
        if not isinstance(payload, dict):
            return self._jsonrpc_error(None, -32600, "JSON-RPC body must be an object")

        request_id = payload.get("id")
        method = payload.get("method")
        # A list or object method is unhashable: leave it to the controller, which rejects it.
        if isinstance(method, str) and method in _SUBSCRIPTION_METHODS:
            if request_id is None:
                return None
            return self._jsonrpc_error(
                request_id, -32601, f"{method} is not supported over the stdio bridge (no server-push channel)"
            )
        if method == "initialize":
            self._initialize_payload = payload
            self._initialized_sent = False
        try:
            response = self.client.post_json(
                payload,
                session_id=None if method == "initialize" else self.session_id,
                protocol_version=self.protocol_version,
            )
            if response.status_code == 404 and method != "initialize" and self._reinitialize():
                # A stdio client never sees the 404 as "re-initialize": to it the
                # server process is alive, so every later call failed with
                # "Unknown MCP session" until the client itself was restarted.
                response = self.client.post_json(
                    payload,
                    session_id=self.session_id,
                    protocol_version=self.protocol_version,
                )
            if method == "notifications/initialized":
                self._initialized_sent = True
        # The answers go to the agent. The answer that no controller is reachable names the URL without its userinfo,
        # query or fragment (W13-1); the invalid-URL answer names no part of it, since a URL that cannot be read
        # cannot be cut safely either (W15-2); and the error text they show has the URL's userinfo, password, query
        # and fragment and any user:password@ removed (W14-1), and the token and each long query value (R5C-20).
        except URLError as exc:
            return self._failed(
                request_id,
                f"No Auto Browser controller reachable at {_display_url(self.client.base_url)} — "
                "start one with 'docker compose up -d' in the auto-browser repo, or pass "
                "--base-url/AUTO_BROWSER_BASE_URL for a remote deployment. "
                f"(underlying: {self._shown(str(exc.reason))})",
            )
        except InvalidControllerURL as exc:
            # A configuration error, not a bridge failure: nothing was sent anywhere (W12-3). Only the controller
            # URL's own: an InvalidURL from elsewhere (an environment proxy's port) is answered as a failure. Its
            # text is a fixed reason in the bridges' own words (W14-1).
            return self._failed(
                request_id,
                "The configured controller URL is not a valid Auto Browser controller URL — fix "
                f"--base-url/AUTO_BROWSER_BASE_URL. (underlying: {exc})",
            )
        except Exception as exc:  # pragma: no cover - defensive bridge guard
            # stderr as the answer: a client can show it to the agent, or keep it in a log the agent reads (R5C-20).
            print(f"stdio bridge unexpected error: {self._shown(str(exc))}", file=self.stderr)
            return self._failed(request_id, f"Unexpected stdio bridge failure: {self._shown(str(exc))}")

        next_session_id = response.headers.get(MCP_SESSION_HEADER.lower())
        if next_session_id:
            self.session_id = next_session_id

        next_protocol_version = response.headers.get(MCP_PROTOCOL_HEADER.lower())
        if next_protocol_version:
            self.protocol_version = next_protocol_version
        elif payload.get("method") == "initialize":
            result = (response.body or {}).get("result") if isinstance(response.body, dict) else None
            if isinstance(result, dict):
                version = result.get("protocolVersion")
                if isinstance(version, str) and version.strip():
                    self.protocol_version = version.strip()

        if payload.get("id") is None:
            # A notification gets no answer, but one the controller refused (a 401 for a bad token, a 5xx) is said on
            # stderr as _failed says one it never got (R5C-23), redacted as an answer is (c4).
            if not 200 <= response.status_code < 300:
                return self._failed(request_id, self._http_error_message(response))
            return None
        if not isinstance(response.body, dict) or "jsonrpc" not in response.body:
            # The HTTP layer's own errors (401 bad token, 400 missing operator
            # header, 429) are {"detail": ...}, not JSON-RPC. Relayed as-is
            # they carried no id, so the client never saw a response to its
            # request and hung without learning why.
            return self._jsonrpc_error(request_id, -32000, self._http_error_message(response))
        body = self._with_error_shown(response.body)
        if method == "initialize":
            return self._without_resource_subscribe(body)
        return body

    def _with_error_shown(self, body: dict[str, Any]) -> dict[str, Any]:
        """``body``, a JSON-RPC answer forwarded to the agent, with its error's message and data shown as the bridges'
        own answers are (R5C-20; the data since c4): a proxy, an MCP gateway or the controller can quote the URL or
        the token in either. The data keeps its shape (``_redacted_value``). Everything else, and a result, goes as
        it came."""
        error = body.get("error")
        if not isinstance(error, dict):
            return body
        shown = dict(error)
        if isinstance(error.get("message"), str):
            shown["message"] = self._shown(error["message"])
        if "data" in error:
            shown["data"] = _redacted_value(error["data"], *self._secrets())
        return body if shown == error else {**body, "error": shown}

    @staticmethod
    def _without_resource_subscribe(body: dict[str, Any]) -> dict[str, Any]:
        """Copy of an initialize response with resources.subscribe switched off."""
        result = body.get("result")
        capabilities = result.get("capabilities") if isinstance(result, dict) else None
        resources = capabilities.get("resources") if isinstance(capabilities, dict) else None
        if not isinstance(resources, dict) or "subscribe" not in resources:
            return body
        return {
            **body,
            "result": {**result, "capabilities": {**capabilities, "resources": {**resources, "subscribe": False}}},
        }

    def _secrets(self) -> tuple[str, str | None]:
        """The base URL and the token the last request carried, whose secrets an answer never shows (R5C-20)."""
        base_url = getattr(self.client, "base_url", "")
        token = getattr(self.client, "last_token", None)
        return (base_url if isinstance(base_url, str) else "", token if isinstance(token, str) else None)

    def _shown(self, text: str) -> str:
        """An error's ``text`` as an answer shows it, without the base URL's known secrets or the token (W14-1)."""
        return _redacted(text, *self._secrets())

    def _shown_detail(self, detail: Any) -> str:
        """A server's error detail as an answer shows it: a structure (a dict or a list) as JSON after
        ``_redacted_value``, where str() wrote it with repr(), which escapes what the redaction looks for (a backslash
        in a decoded query); anything else as its text through ``_shown`` (R5C-20)."""
        if isinstance(detail, (dict, list)):
            return json.dumps(_redacted_value(detail, *self._secrets()), ensure_ascii=False)
        return self._shown(str(detail))

    def _http_error_message(self, response: HttpMcpResponse) -> str:
        if response.body is None:
            return f"Empty or non-JSON response from Auto Browser MCP endpoint ({response.status_code})"
        detail = response.body.get("detail") if isinstance(response.body, dict) else None
        message = f"Auto Browser controller returned HTTP {response.status_code}"
        if detail:
            message += f": {self._shown_detail(detail)}"
        if response.status_code == 401:
            message += (
                " (set --token-file or AUTO_BROWSER_TOKEN_FILE to the controller's token file, data/api-token in"
                " your auto-browser folder, or --bearer-token or AUTO_BROWSER_BEARER_TOKEN to the token itself)"
            )
            problem = getattr(self.client, "token_file_problem", None)
            if problem:
                message += f"; {self._shown(str(problem))}"
        elif response.status_code == 400 and "operator" in str(detail).lower():
            message += (
                " (this controller requires an operator id on every request: pass --operator-id or set"
                " AUTO_BROWSER_OPERATOR_ID, or turn off REQUIRE_OPERATOR_ID on the controller)"
            )
        elif response.status_code == 429:
            # Seconds only (W15-6): an HTTP-date, or any other text, which can quote the URL, is not shown.
            retry_after = (response.headers.get("retry-after") or "").strip()
            # Codex R03: not when it is a secret of the URL, such as a numeric password or fragment.
            if retry_after.isascii() and retry_after.isdigit() and self._shown(retry_after) == retry_after:
                message += f" (rate limited; retry after {retry_after}s)"
            else:
                message += " (rate limited)"
        return message

    def _reinitialize(self) -> bool:
        """Open a fresh controller session with the client's own initialize.

        Returns whether the session was re-established; if not, the caller
        relays the original 404.
        """
        if self._initialize_payload is None or self.session_id is None:
            return False
        replay = {**self._initialize_payload, "id": "auto-browser-bridge-reinitialize"}
        response = self.client.post_json(replay, session_id=None, protocol_version=None)
        next_session_id = response.headers.get(MCP_SESSION_HEADER.lower())
        if response.status_code != 200 or not next_session_id:
            return False
        previous = (self.session_id, self.protocol_version)
        self.session_id = next_session_id
        self.protocol_version = response.headers.get(MCP_PROTOCOL_HEADER.lower()) or self.protocol_version
        if self._initialized_sent:
            # The controller refuses tool calls on a session that never got this
            # notification, so a lost ack (429, 5xx, a dropped connection) leaves a
            # half-open session. Treating that as success made the retried call
            # fail with "Session not initialized" instead of the real cause.
            try:
                ack = self.client.post_json(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    session_id=self.session_id,
                    protocol_version=self.protocol_version,
                )
            except Exception:
                self._abandon_reinitialized_session(previous)
                raise
            if not 200 <= ack.status_code < 300:
                print(
                    f"stdio bridge: re-initialize failed, notifications/initialized got HTTP {ack.status_code}",
                    file=self.stderr,
                )
                self._abandon_reinitialized_session(previous)
                return False
        print("stdio bridge: controller forgot the MCP session; re-initialized", file=self.stderr)
        return True

    def _abandon_reinitialized_session(self, previous: tuple[str | None, str | None]) -> None:
        """Drop the half-open session and go back to the old id, so the next call retries."""
        self.client.delete_session(session_id=self.session_id)
        self.session_id, self.protocol_version = previous

    def _failed(self, request_id: Any, message: str) -> dict[str, Any] | None:
        """The answer to a request the controller never answered: a JSON-RPC error carrying ``message``. A notification
        (no id) gets none here either, as it gets none once the controller has answered it: its ``message`` goes to
        stderr instead (R5C-23)."""
        if request_id is None:
            print(f"stdio bridge: {message}", file=self.stderr)
            return None
        return self._jsonrpc_error(request_id, -32000, message)

    @staticmethod
    def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message,
            },
        }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bridge stdio MCP clients to the Auto Browser HTTP MCP endpoint.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AUTO_BROWSER_BASE_URL", "http://127.0.0.1:8000/mcp"),
        help="HTTP MCP endpoint to proxy to; env AUTO_BROWSER_BASE_URL (default: %(default)s)",
    )
    parser.add_argument(
        "--bearer-token",
        default=os.environ.get("AUTO_BROWSER_BEARER_TOKEN"),
        help="The controller's API token; env AUTO_BROWSER_BEARER_TOKEN. Wins over --token-file.",
    )
    parser.add_argument(
        "--token-file",
        default=os.environ.get("AUTO_BROWSER_TOKEN_FILE"),
        help=(
            "File holding the controller's API token, read on every request: a local controller writes it to"
            " data/api-token in the auto-browser folder; env AUTO_BROWSER_TOKEN_FILE."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.environ.get("AUTO_BROWSER_HTTP_TIMEOUT_SECONDS", "60")),
        help="Per-request timeout in seconds; env AUTO_BROWSER_HTTP_TIMEOUT_SECONDS (default: %(default)s).",
    )
    parser.add_argument(
        "--operator-id",
        default=os.environ.get("AUTO_BROWSER_OPERATOR_ID"),
        help="Sent as X-Operator-Id, for a controller with REQUIRE_OPERATOR_ID=true; env AUTO_BROWSER_OPERATOR_ID.",
    )
    return parser


def _utf8_stdio() -> None:
    """Speak UTF-8 on stdin and stdout whatever the console's code page.

    MCP's stdio transport is UTF-8. A client that launches the bridge with pipes
    on Windows hands it the locale's code page (cp1252) for both streams, so
    tools/list went out as invalid UTF-8 and a page with text outside cp1252
    crashed the bridge mid-session, leaving its browser session open. Undecodable
    input becomes U+FFFD instead of a crash, and a lone surrogate from a page is
    written as its JSON escape, which decodes back to it.
    """
    for stream, errors in ((sys.stdin, "replace"), (sys.stdout, "backslashreplace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors=errors)
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")


def main() -> int:
    args = build_arg_parser().parse_args()
    _utf8_stdio()
    bridge = StdioMcpBridge(
        client=HttpMcpClient(
            base_url=args.base_url,
            bearer_token=args.bearer_token,
            timeout_seconds=args.timeout_seconds,
            operator_id=args.operator_id,
            token_file=args.token_file,
        )
    )
    return bridge.run()


if __name__ == "__main__":
    raise SystemExit(main())
