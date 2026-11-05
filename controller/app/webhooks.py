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
_DISALLOWED_WEBHOOK_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
}
_RESOLVE_TIMEOUT_SECONDS = 5.0


def _address_is_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved


def _is_disallowed_webhook_name(host: str) -> bool:
    normalized = host.lower().strip().rstrip(".")
    return normalized in _DISALLOWED_WEBHOOK_HOSTS or normalized.endswith(".localhost")


def _loggable(webhook_url: str) -> str:
    """scheme://host of a webhook URL: its path, query and userinfo often carry the secret that authorises it."""
    try:
        parsed = urlsplit(webhook_url)
        return f"{parsed.scheme}://{parsed.hostname or '?'}"
    except ValueError:
        return "<unparseable webhook url>"


@dataclass(frozen=True)
class _Target:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    extensions: dict[str, str] = field(default_factory=dict)


async def _resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
        timeout=_RESOLVE_TIMEOUT_SECONDS,
    )
    return list(dict.fromkeys(info[4][0] for info in infos))


async def _pinned_target(webhook_url: str, *, allow_private: bool) -> _Target | None:
    """Where to send the request, or None (after a warning) when the target is refused.

    The host is read as a browser reads it (numeric spellings of loopback are refused), resolved here, every
    answer must be a public address, and the request goes to the first one with the original name kept in the
    Host header and the TLS server name, so a second lookup cannot return a different address.
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
    if not allow_private and _is_disallowed_webhook_name(host):
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
    if not addresses or (not allow_private and any(_address_is_disallowed(ipaddress.ip_address(a)) for a in addresses)):
        logger.warning("blocked disallowed approval webhook target: %s", label)
        return None
    if literal is not None:
        return _Target(url=webhook_url)

    pinned = addresses[0]
    userinfo, _, authority = parsed.netloc.rpartition("@")
    pinned_host = f"[{pinned}]" if ":" in pinned else pinned
    netloc = (f"{userinfo}@" if userinfo else "") + pinned_host + (f":{port}" if port is not None else "")
    return _Target(
        url=parsed._replace(netloc=netloc).geturl(),
        headers={"Host": authority},
        extensions={"sni_hostname": host} if parsed.scheme == "https" else {},
    )


def get_client() -> httpx.AsyncClient:
    global _client
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
    target = await _pinned_target(webhook_url, allow_private=allow_private_targets)
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
        **target.headers,
    }
    if webhook_secret:
        headers["X-Webhook-Signature"] = _sign(raw, webhook_secret)

    try:
        resp = await get_client().post(target.url, content=raw, headers=headers, extensions=target.extensions)
        if resp.status_code >= 400:
            logger.warning("webhook %s returned %d", _loggable(webhook_url), resp.status_code)
    except Exception as exc:
        logger.warning("webhook dispatch to %s failed: %s", _loggable(webhook_url), type(exc).__name__)
