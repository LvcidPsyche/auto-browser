"""Stdio <-> HTTP bridge for Auto Browser's MCP endpoint.

Single-file and stdlib-only by design. This module ships in two places:
as ``auto_browser_client.mcp_bridge`` (the PyPI ``auto-browser-mcp`` console
script) and as ``app.mcp_stdio`` inside the controller image (Glama/Docker
entrypoint). A guard test asserts the two copies stay byte-identical, so any
edit here must be mirrored to the other copy.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
from dataclasses import dataclass
from http.client import HTTPConnection, InvalidURL
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

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


_DIRECT = build_opener(ProxyHandler({}), _NoRedirects)


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


def _goes_direct(request: Request) -> bool:
    """Whether the request goes past any environment proxy: any of four readings of its host is loopback (W11-1).

    The readings: (1) the host http.client connects to, which reads an unbracketed ::1:8000 as ::1; (2) the host of
    urllib's percent-decoded ``request.host`` read as a URL authority (W9-5: %31%32%37.0.0.2 is 127.0.0.2), which
    drops a userinfo http.client keeps in its host; (3) the host of the URL as written, which a proxy reads, so a
    percent-encoded / ? or # in a userinfo (x%2f@127.0.0.1) cannot hide a loopback host from this rule; (4) urllib's
    decoded ``request.host`` as a whole (W12-2), so an unbracketed ::1 or :: without a port, which http.client reads
    as host ":" and urlsplit as no host, is local (a host:port form is no address and is left to the others). Going
    direct only ever sends the request to the host named or fails; a host remote all four ways still uses the proxy.
    Constructing the HTTPConnection parses and does not connect; its InvalidURL (a non-numeric port) propagates,
    and a reading urlsplit cannot parse (%5b127.0.0.1%5d, x%5b@127.0.0.1) raises InvalidURL from its ValueError
    (W12-3), so a malformed authority is refused before any opener is chosen and the request is sent nowhere. A
    host-less request (``request.host`` None) is not direct and falls through to urllib's own error, raised before
    any proxy is used (W10-14).
    """
    if request.host is None:
        return False
    try:
        readings = (
            HTTPConnection(request.host).host,
            urlsplit("//" + request.host).hostname,
            urlsplit(request.full_url).hostname,
            request.host,
        )
    except ValueError as exc:
        raise InvalidURL(str(exc)) from exc
    return any(_is_loopback_host(host) for host in readings)


# Module-level, as urllib's own urlopen was, so tests can patch it.
def urlopen(request: Request, *args: Any, **kwargs: Any) -> Any:
    """Open the request without following redirects, past any environment proxy when its host is loopback.

    urllib skips a proxy for localhost only when no_proxy names it, so with http_proxy set the bridge's
    Authorization header for a local controller went to the proxy. Any other host still uses the proxy the
    environment names, read for each request. ``_goes_direct`` decides, from four readings of the host.
    """
    if _goes_direct(request):
        return _DIRECT.open(request, *args, **kwargs)
    return build_opener(_NoRedirects).open(request, *args, **kwargs)


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
        # Why the last request went without a token from token_file; added to a 401's message.
        self.token_file_problem: str | None = None

    def _bearer(self) -> str | None:
        """The token to send: bearer_token when set, else the one in token_file, read now.

        Read on every request, because a stdio client starts the bridge before the
        controller has written the file, and a regenerated token must not need a
        restart. A missing or unreadable file sends no header, so a controller
        running without a token still works.
        """
        if self.bearer_token or not self.token_file:
            return self.bearer_token
        try:
            with open(self.token_file, encoding="utf-8") as handle:
                token = handle.read().strip()
        except (OSError, UnicodeDecodeError) as exc:
            self.token_file_problem = f"could not read the token file {self.token_file}: {exc}"
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
        request = Request(self.base_url, data=payload, headers=headers, method=method)
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
        except ValueError:
            # A proxy's HTML error page, say. Reported with its status below.
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
        except URLError as exc:
            return self._jsonrpc_error(
                request_id,
                -32000,
                f"No Auto Browser controller reachable at {self.client.base_url} — "
                "start one with 'docker compose up -d' in the auto-browser repo, or pass "
                f"--base-url/AUTO_BROWSER_BASE_URL for a remote deployment. (underlying: {exc.reason})",
            )
        except InvalidURL as exc:
            # A configuration error, not a bridge failure: nothing was sent anywhere (W12-3).
            return self._jsonrpc_error(
                request_id,
                -32000,
                f"{self.client.base_url} is not a valid Auto Browser controller URL — fix "
                f"--base-url/AUTO_BROWSER_BASE_URL. (underlying: {exc})",
            )
        except Exception as exc:  # pragma: no cover - defensive bridge guard
            print(f"stdio bridge unexpected error: {exc}", file=self.stderr)
            return self._jsonrpc_error(request_id, -32000, f"Unexpected stdio bridge failure: {exc}")

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
            return None
        if not isinstance(response.body, dict) or "jsonrpc" not in response.body:
            # The HTTP layer's own errors (401 bad token, 400 missing operator
            # header, 429) are {"detail": ...}, not JSON-RPC. Relayed as-is
            # they carried no id, so the client never saw a response to its
            # request and hung without learning why.
            return self._jsonrpc_error(request_id, -32000, self._http_error_message(response))
        if method == "initialize":
            return self._without_resource_subscribe(response.body)
        return response.body

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

    def _http_error_message(self, response: HttpMcpResponse) -> str:
        if response.body is None:
            return f"Empty or non-JSON response from Auto Browser MCP endpoint ({response.status_code})"
        detail = response.body.get("detail") if isinstance(response.body, dict) else None
        message = f"Auto Browser controller returned HTTP {response.status_code}"
        if detail:
            message += f": {detail}"
        if response.status_code == 401:
            message += (
                " (set --token-file or AUTO_BROWSER_TOKEN_FILE to the controller's token file, data/api-token in"
                " your auto-browser folder, or --bearer-token or AUTO_BROWSER_BEARER_TOKEN to the token itself)"
            )
            problem = getattr(self.client, "token_file_problem", None)
            if problem:
                message += f"; {problem}"
        elif response.status_code == 400 and "operator" in str(detail).lower():
            message += (
                " (this controller requires an operator id on every request: pass --operator-id or set"
                " AUTO_BROWSER_OPERATOR_ID, or turn off REQUIRE_OPERATOR_ID on the controller)"
            )
        elif response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            message += f" (rate limited; retry after {retry_after}s)" if retry_after else " (rate limited)"
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
