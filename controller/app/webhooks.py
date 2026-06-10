"""
webhooks.py — Approval webhook dispatcher for auto-browser.

When APPROVAL_WEBHOOK_URL is configured, POSTs a signed JSON payload
every time an approval is created or its status changes.

Signature: X-Webhook-Signature: sha256=<hex>
The signature covers the raw request body using HMAC-SHA256 with
APPROVAL_WEBHOOK_SECRET as the key (Slack-compatible format).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from .models import ApprovalRecord

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_DISALLOWED_WEBHOOK_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
}


def _is_disallowed_webhook_host(host: str) -> bool:
    normalized = host.lower().strip()
    if normalized in _DISALLOWED_WEBHOOK_HOSTS or normalized.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
    )


def _is_disallowed_webhook_url(webhook_url: str) -> bool:
    try:
        parsed = urlsplit(webhook_url)
    except Exception:
        return True
    if parsed.scheme not in {"http", "https"}:
        return True
    if not parsed.hostname:
        return True
    return _is_disallowed_webhook_host(parsed.hostname)


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
) -> None:
    """Fire-and-forget: POST approval event to webhook_url."""
    if _is_disallowed_webhook_url(webhook_url):
        logger.warning("blocked disallowed approval webhook target: %s", webhook_url)
        return

    body: dict[str, Any] = {
        "event": "approval",
        "approval_id": approval.id,
        "session_id": approval.session_id,
        "kind": approval.kind,
        "status": approval.status,
        "reason": approval.reason,
        "created_at": approval.created_at,
        "updated_at": approval.updated_at,
    }
    if extra:
        body.update(extra)

    raw = json.dumps(body, separators=(",", ":")).encode()
    headers: dict[str, str] = {"Content-Type": "application/json", "User-Agent": "auto-browser/1.2.1"}
    if webhook_secret:
        headers["X-Webhook-Signature"] = _sign(raw, webhook_secret)

    try:
        resp = await get_client().post(webhook_url, content=raw, headers=headers)
        if resp.status_code >= 400:
            logger.warning("webhook %s returned %d", webhook_url, resp.status_code)
    except Exception as exc:
        logger.warning("webhook dispatch failed: %s", exc)
