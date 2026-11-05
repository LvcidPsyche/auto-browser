"""Header masking went by an exact-name list, so any other secret-bearing header was stored in clear."""

from __future__ import annotations

import pytest

from app.network_inspector import _mask_sensitive_headers

MASKED = "[MASKED]"
VALUE = "SECRETVALUE"

SECRET_HEADERS = [
    "Authorization",
    "Proxy-Authorization",
    "X-Auth-Token",
    "X-CSRF-Token",
    "X-Amz-Security-Token",
    "X-Api-Key",
    "X-Access-Token",
    "Api-Key",
    "X-Goog-Api-Key",
    "Private-Token",
    "X-Vault-Token",
    "X-GitHub-Token",
    "X-Session-Id",
    "X-Shopify-Access-Token",
    "X-Slack-Signature",
    "X-Auth",
    "X-AccessToken",
    "X-App_Secret",
    "X-User-Password",
    "XApiKey",
    "Set-Cookie",
]

HARMLESS_HEADERS = [
    ":authority",
    "Keep-Alive",
    "Accept",
    "Accept-Encoding",
    "Accept-Language",
    "Content-Type",
    "Content-Length",
    "User-Agent",
    "Cache-Control",
    "Host",
]


@pytest.mark.parametrize("name", SECRET_HEADERS)
def test_secret_bearing_header_names_are_masked(name: str) -> None:
    masked, _ = _mask_sensitive_headers({name: VALUE})

    assert masked[name] == MASKED


@pytest.mark.parametrize("name", HARMLESS_HEADERS)
def test_ordinary_headers_are_left_alone(name: str) -> None:
    masked, _ = _mask_sensitive_headers({name: VALUE})

    assert masked[name] == VALUE
