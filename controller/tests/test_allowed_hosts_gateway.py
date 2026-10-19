"""ALLOWED_HOSTS applies to what the MCP tools do to a session, not only to where its page goes.

set_cookies wrote cookies for any domain, so a caller confined to one host could
plant a session cookie for another and fixate it. And the tools that read the page
returned whatever it showed, even after the page had moved itself onto a host the
list refuses. No browser is needed here: the gateway runs against a real
BrowserManager's allowlist and a mocked context, or a mocked page.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.browser_manager import BrowserManager
from app.tool_gateway import McpToolGateway
from app.tool_inputs import (
    DragDropInput,
    EvalJsInput,
    FindElementsInput,
    GetPageHtmlInput,
    GetStorageInput,
    SetCookiesInput,
    SetStorageInput,
    WaitForSelectorInput,
)
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


# ── the page-content tools check the page before they read it ────────────────
# Real-browser coverage is in test_allowed_hosts_navigation.py; this runs without Chromium.


def _page_tool_gateway() -> tuple[McpToolGateway, SimpleNamespace]:
    page = SimpleNamespace(
        evaluate=AsyncMock(),
        content=AsyncMock(),
        wait_for_selector=AsyncMock(),
        locator=lambda selector: SimpleNamespace(first=SimpleNamespace(bounding_box=AsyncMock())),
        mouse=SimpleNamespace(move=AsyncMock(), down=AsyncMock(), up=AsyncMock()),
    )
    refusal = PermissionError("The page navigated on its own to a host ALLOWED_HOSTS does not allow")
    manager = SimpleNamespace(
        get_session=AsyncMock(return_value=SimpleNamespace(page=page)),
        _assert_page_allowed=Mock(side_effect=refusal),
    )
    gateway = McpToolGateway(manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())
    return gateway, page


PAGE_TOOLS = {
    "eval_js": ("_eval_js", EvalJsInput(session_id="s1", expression="1")),
    "wait_for_selector": ("_wait_for_selector", WaitForSelectorInput(session_id="s1", selector="p")),
    "get_html": ("_get_html", GetPageHtmlInput(session_id="s1")),
    "get_text": ("_get_html", GetPageHtmlInput(session_id="s1", text_only=True)),
    "find_by_query": ("_find_elements", FindElementsInput(session_id="s1", query="x")),
    "find_by_selector": ("_find_elements", FindElementsInput(session_id="s1", selector="p")),
    "drag_drop": ("_drag_drop", DragDropInput(session_id="s1", source_selector="a", target_selector="b")),
    "get_storage": ("_get_local_storage", GetStorageInput(session_id="s1")),
    "set_storage": ("_set_local_storage", SetStorageInput(session_id="s1", key="k", value="v")),
}


@pytest.mark.parametrize("tool", sorted(PAGE_TOOLS))
async def test_page_tools_refuse_before_touching_the_page(tool: str) -> None:
    gateway, page = _page_tool_gateway()
    handler, payload = PAGE_TOOLS[tool]

    with pytest.raises(PermissionError, match="ALLOWED_HOSTS"):
        await getattr(gateway, handler)(payload)

    page.evaluate.assert_not_called()
    page.content.assert_not_called()
    page.wait_for_selector.assert_not_called()
    page.mouse.move.assert_not_called()
