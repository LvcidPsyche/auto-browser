from __future__ import annotations

import ipaddress
import os
from pathlib import Path

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


def loopback_transport(base_url: str, *, asynchronous: bool) -> httpx.BaseTransport | httpx.AsyncBaseTransport | None:
    """A transport of its own for a loopback controller, else None (W7-1).

    httpx follows HTTP_PROXY, http_proxy and ALL_PROXY to a loopback host unless NO_PROXY names it, so the
    Authorization header for a local controller went to the proxy. A client given a transport uses no
    environment proxy, and the transport still reads SSL_CERT_FILE and SSL_CERT_DIR (trust_env=False would drop
    those too). Any other host keeps the proxy the environment names.
    """
    host = httpx.URL(base_url).host
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        return None
    return httpx.AsyncHTTPTransport() if asynchronous else httpx.HTTPTransport()
