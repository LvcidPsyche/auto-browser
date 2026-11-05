"""
webhooks.py — Approval webhook dispatcher for auto-browser.

When APPROVAL_WEBHOOK_URL is configured, POSTs a signed JSON payload
every time an approval is created or its status changes.

Signature: X-Webhook-Signature: sha256=<hex>
The signature covers the raw request body using HMAC-SHA256 with
APPROVAL_WEBHOOK_SECRET as the key (Slack-compatible format).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from .approval_display import approval_summary, shown_code_points
from .models import ApprovalRecord
from .url_safety import refuse_ambiguous_host
from .version import __version__

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_direct_client: httpx.AsyncClient | None = None
_DISALLOWED_WEBHOOK_HOSTS = {
    "localhost",
    "localhost.localdomain",
}
# Cloud metadata: refused even when APPROVAL_WEBHOOK_ALLOW_PRIVATE_TARGETS opens loopback and private targets.
_METADATA_HOSTS = frozenset({"metadata", "metadata.google.internal"})
_METADATA_NETWORKS = tuple(ipaddress.ip_network(net) for net in ("169.254.0.0/16", "fe80::/10", "fd00:ec2::254/128"))
_RESOLVE_TIMEOUT_SECONDS = 5.0


def _address_is_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private: bool) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if any(ip in network for network in _METADATA_NETWORKS):
        return True
    if allow_private:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved


def _is_disallowed_webhook_name(host: str, *, allow_private: bool) -> bool:
    normalized = host.lower().strip().rstrip(".")
    if normalized in _METADATA_HOSTS:
        return True
    return not allow_private and (normalized in _DISALLOWED_WEBHOOK_HOSTS or normalized.endswith(".localhost"))


def _loggable(webhook_url: str) -> str:
    """scheme://host of a webhook URL: its path, query and userinfo often carry the secret that authorises it."""
    try:
        parsed = urlsplit(webhook_url)
        return f"{parsed.scheme}://{parsed.hostname or '?'}"
    except ValueError:
        return "<unparseable webhook url>"


@dataclass(frozen=True)
class _Target:
    url: str  # as written: what an IP literal, or a name sent through a proxy, is posted to
    pinned_urls: tuple[str, ...] = ()  # the same URL aimed at each checked address, in the resolver's order
    headers: dict[str, str] = field(default_factory=dict)  # the pinned request's Host
    extensions: dict[str, str] = field(default_factory=dict)  # the pinned request's TLS server name


async def _resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
        timeout=_RESOLVE_TIMEOUT_SECONDS,
    )
    return list(dict.fromkeys(info[4][0] for info in infos))


def _env_proxy_applies(url: str) -> bool:
    """Whether httpx sends `url` through a proxy from the environment (HTTP(S)_PROXY or ALL_PROXY, less NO_PROXY).

    Asked of the client that sends a request by name, so the answer is httpx's own routing for that URL; httpx 0.28
    has no public call for it.
    """
    client = get_client()
    return client._transport_for_url(httpx.URL(url)) is not client._transport


async def _checked_target(webhook_url: str, *, allow_private: bool) -> _Target | None:
    """Where to send the request, or None (after a warning) when the target is refused.

    The host is read as a browser reads it (numeric spellings of loopback are refused), resolved here, and every
    answer must be a public address; `allow_private` admits any answer but cloud metadata (169.254.0.0/16, fe80::/10,
    fd00:ec2::254, and the names metadata and metadata.google.internal, refused before any lookup). With no proxy the
    request goes to the checked answers (pinned_urls), with the original name kept in the Host header and the TLS
    server name, so a second lookup cannot return a different address; dispatch_approval_event decides that.
    """
    label = _loggable(webhook_url)
    try:
        parsed = urlsplit(webhook_url)
        refuse_ambiguous_host(webhook_url, field_name="approval webhook URL")
        port = parsed.port
    except ValueError:
        logger.warning("blocked disallowed approval webhook target: %s", label)
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        logger.warning("blocked disallowed approval webhook target: %s", label)
        return None
    host = parsed.hostname.rstrip(".")
    if _is_disallowed_webhook_name(host, allow_private=allow_private):
        logger.warning("blocked disallowed approval webhook target: %s", label)
        return None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            addresses = await _resolve_host(host, port or (443 if parsed.scheme == "https" else 80))
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning("approval webhook host did not resolve: %s (%s)", label, type(exc).__name__)
            return None
    if not addresses or any(
        _address_is_disallowed(ipaddress.ip_address(a), allow_private=allow_private) for a in addresses
    ):
        logger.warning("blocked disallowed approval webhook target: %s", label)
        return None
    if literal is not None:
        return _Target(url=webhook_url)

    userinfo, _, authority = parsed.netloc.rpartition("@")
    pinned_urls = []
    for address in addresses:
        pinned_host = f"[{address}]" if ":" in address else address
        netloc = (f"{userinfo}@" if userinfo else "") + pinned_host + (f":{port}" if port is not None else "")
        pinned_urls.append(parsed._replace(netloc=netloc).geturl())
    return _Target(
        url=webhook_url,
        pinned_urls=tuple(pinned_urls),
        headers={"Host": authority},
        extensions={"sni_hostname": host} if parsed.scheme == "https" else {},
    )


async def _post_pinned(target: _Target, raw: bytes, headers: dict[str, str]) -> httpx.Response:
    """Post to the checked addresses in turn, moving on only when one cannot be connected to.

    Nothing was sent to an address that did not connect, so the next one is tried, as httpx's own lookup would; once
    one connects, its answer (an HTTP error included) is final.
    """
    client = get_client(direct=True)
    pinned_headers = {**headers, **target.headers}
    *earlier, last = target.pinned_urls
    for pinned_url in earlier:
        try:
            return await client.post(pinned_url, content=raw, headers=pinned_headers, extensions=target.extensions)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            label = _loggable(target.url)
            logger.info(
                "webhook %s: a checked address did not connect (%s); trying the next", label, type(exc).__name__
            )
    return await client.post(last, content=raw, headers=pinned_headers, extensions=target.extensions)


def get_client(*, direct: bool = False) -> httpx.AsyncClient:
    """The client for a request sent by name, which takes its proxies from the environment; or, `direct`, the one for
    a request pinned to a checked address, which never uses a proxy (NO_PROXY spares the name, not the address).

    The direct client's transport of its own turns httpx's proxies from the environment off; that transport still
    reads SSL_CERT_FILE and SSL_CERT_DIR, as the other client does.
    """
    global _client, _direct_client
    if direct:
        if _direct_client is None or _direct_client.is_closed:
            _direct_client = httpx.AsyncClient(timeout=10.0, transport=httpx.AsyncHTTPTransport())
        return _direct_client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=10.0)
    return _client


def _sign(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


async def dispatch_approval_event(
    approval: ApprovalRecord,
    *,
    webhook_url: str,
    webhook_secret: str | None = None,
    extra: dict[str, Any] | None = None,
    allow_private_targets: bool = False,
) -> None:
    """Fire-and-forget: POST approval event to webhook_url."""
    target = await _checked_target(webhook_url, allow_private=allow_private_targets)
    if target is None:
        return

    body: dict[str, Any] = {
        "event": "approval",
        "approval_id": approval.id,
        "session_id": approval.session_id,
        "kind": approval.kind,
        "status": approval.status,
        # The reason is agent-written for an action approval; these two are the server's.
        "origin": approval.origin,
        "action_summary": approval_summary(approval),
        # Agent-written: shown with its code points, as the dashboard shows it.
        "reason": shown_code_points(approval.reason),
        # A tool call's whole arguments stay on the controller (GET /approvals/{id}/arguments).
        "arguments_size": approval.arguments_size,
        "created_at": approval.created_at,
        "updated_at": approval.updated_at,
    }
    if extra:
        body.update(extra)

    raw = json.dumps(body, separators=(",", ":")).encode()
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": f"auto-browser/{__version__}",
    }
    if webhook_secret:
        headers["X-Webhook-Signature"] = _sign(raw, webhook_secret)

    try:
        if not target.pinned_urls or _env_proxy_applies(target.url):
            # An IP literal goes as written. Through a proxy from the environment the request goes by name: a CONNECT
            # tunnel takes its TLS name from the URL and NO_PROXY is written by name, so the proxy then owns
            # resolution (the answers were still checked above).
            resp = await get_client().post(target.url, content=raw, headers=headers)
        else:
            resp = await _post_pinned(target, raw, headers)
        if resp.status_code >= 400:
            logger.warning("webhook %s returned %d", _loggable(webhook_url), resp.status_code)
    except Exception as exc:
        logger.warning("webhook dispatch to %s failed: %s", _loggable(webhook_url), type(exc).__name__)
