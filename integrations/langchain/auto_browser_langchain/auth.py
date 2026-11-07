from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from typing import Literal, overload

import httpx


def resolve_bearer_token(bearer_token: str | None, token_file: str | os.PathLike[str] | None) -> str | None:
    """The token to send: ``bearer_token`` when set, else the one in ``token_file``, read now.

    A local controller with no API_BEARER_TOKEN writes its generated token to
    data/api-token in the auto-browser folder. Callers read the file on every
    request, so a tool built before the controller first started, or kept across
    a regenerated token, sends the current one. A missing or empty file raises:
    the caller asked for a token, and a request without one would only 401.
    """
    token = (bearer_token or "").strip() or None
    if token or token_file is None:
        return token
    token_file = os.path.expanduser(token_file)  # "~/auto-browser/data/api-token", as the stdio bridge takes it
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"token_file {token_file} does not exist: start the controller first, which writes its "
            "token to data/api-token in the auto-browser folder"
        ) from exc
    except PermissionError as exc:
        raise PermissionError(
            f"cannot read token_file {token_file}: {exc.strerror}. Run as the user that owns the auto-browser "
            "folder's data/, or read the token with: docker compose exec -T controller cat /data/api-token"
        ) from exc
    if not token:
        raise ValueError(f"token_file {token_file} is empty")
    return token


# One overload per client kind (W9-12): httpx.Client takes only a BaseTransport and httpx.AsyncClient only an
# AsyncBaseTransport, so the union return type was flagged at every call site.
@overload
def loopback_transport(base_url: str, *, asynchronous: Literal[True]) -> httpx.AsyncBaseTransport | None: ...


@overload
def loopback_transport(base_url: str, *, asynchronous: Literal[False]) -> httpx.BaseTransport | None: ...


def loopback_transport(base_url: str, *, asynchronous: bool) -> httpx.BaseTransport | httpx.AsyncBaseTransport | None:
    """A transport of its own for a loopback controller, else None (W7-1).

    httpx follows HTTP_PROXY, http_proxy and ALL_PROXY to a loopback host unless NO_PROXY names it, so the
    Authorization header for a local controller went to the proxy. A client given a transport uses no
    environment proxy, and the transport still reads SSL_CERT_FILE and SSL_CERT_DIR (trust_env=False would drop
    those too). Any other host keeps the proxy the environment names. The rule is the bridge's (W8-3, W9-5):
    localhost, also with one trailing dot, or an address that is loopback or unspecified (0.0.0.0 and ::, which reach
    this host on Linux and macOS), an IPv4-mapped IPv6 address counting as the IPv4 address it maps. The address is
    the IP literal, or what the platform's numeric parser reads the host as (127.2, 2130706434 and 0x7f000002 are
    127.0.0.2 to the socket layer), never a DNS answer.
    """
    if not _is_loopback_host(httpx.URL(base_url).host):
        return None
    return httpx.AsyncHTTPTransport() if asynchronous else httpx.HTTPTransport()


def _is_loopback_host(host: str) -> bool:
    """Whether a request to this host stays on this machine, by the bridges' rule.

    The name compared is the one the socket layer resolves (W12-1, W13-4): the host encoded with the ``idna``
    codec, as getaddrinfo encodes it, and lowercased, so local<U+00AD>host and LOCALHOST are localhost; a host the
    codec refuses is compared as given.
    """
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
