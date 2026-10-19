"""ALLOWED_HOSTS applies to what the MCP tools do to a session, not only to where its page goes.

set_cookies wrote cookies for any domain, so a caller confined to one host could
plant a session cookie for another and fixate it. No browser is needed here: the
gateway runs against a real BrowserManager's allowlist and a mocked context.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.browser_manager import BrowserManager
from app.tool_gateway import McpToolGateway
from app.tool_inputs import SetCookiesInput
from tests.test_browser_manager_create_session import _settings


def _gateway(tmp_path: Path, allowed_hosts: str) -> tuple[McpToolGateway, AsyncMock]:
    settings = _settings(tmp_path)
    settings.allowed_hosts = allowed_hosts
    manager = BrowserManager(settings)
    context = SimpleNamespace(add_cookies=AsyncMock())
    manager.get_session = AsyncMock(return_value=SimpleNamespace(context=context))  # type: ignore[method-assign]
    gateway = McpToolGateway(manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())
    return gateway, context.add_cookies


def _cookies(*cookies: dict) -> SetCookiesInput:
    return SetCookiesInput(session_id="s1", cookies=list(cookies))


ALLOWED_COOKIE = {"name": "a", "value": "1", "url": "https://allowed.example/"}


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "b", "value": "2", "url": "https://evil.example/"},
        {"name": "b", "value": "2", "domain": "evil.example", "path": "/"},
        {"name": "b", "value": "2", "domain": ".evil.example", "path": "/"},
    ],
)
async def test_set_cookies_refuses_a_cookie_for_a_disallowed_host_and_adds_none(tmp_path: Path, bad: dict) -> None:
    gateway, add_cookies = _gateway(tmp_path, "allowed.example")

    with pytest.raises(PermissionError, match="evil.example"):
        await gateway._set_cookies(_cookies(ALLOWED_COOKIE, bad))

    add_cookies.assert_not_called()


async def test_set_cookies_accepts_cookies_for_allowed_hosts(tmp_path: Path) -> None:
    gateway, add_cookies = _gateway(tmp_path, "allowed.example")
    by_domain = {"name": "b", "value": "2", "domain": ".allowed.example", "path": "/"}

    result = await gateway._set_cookies(_cookies(ALLOWED_COOKIE, by_domain))

    assert result == {"session_id": "s1", "set": 2}
    add_cookies.assert_awaited_once_with([ALLOWED_COOKIE, by_domain])


async def test_set_cookies_is_unrestricted_when_allowed_hosts_is_a_wildcard(tmp_path: Path) -> None:
    gateway, add_cookies = _gateway(tmp_path, "*")
    anywhere = {"name": "b", "value": "2", "domain": "evil.example", "path": "/"}

    await gateway._set_cookies(_cookies(anywhere))

    add_cookies.assert_awaited_once()
