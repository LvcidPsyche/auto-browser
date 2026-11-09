"""NAV-12: browser.get_html(text_only=true) and browser.find_elements read the page in the controller's isolated world.

They ran in the page's own JavaScript world, so a page that replaced a DOM getter or a built-in gave them its own text
and elements, and saw what the agent searched for: the query went through its String.prototype.toLowerCase, the
selector through its Document.prototype.querySelectorAll. The observation's scripts already ran in the isolated world
(app/browser/element_ids.py), which shares the page's DOM and none of its JavaScript. Real Chromium, as only it has
worlds; the gateway's own handlers are called, on a session the real BrowserManager opened.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import McpToolCallRequest
from app.tool_gateway import McpToolGateway
from app.tool_inputs import FindElementsInput, GetPageHtmlInput
from tests._chromium import requires_chromium
from tests.test_allowed_hosts_navigation import (  # noqa: F401 - _shared_chromium is the module's Chromium fixture
    _shared_chromium,
    browser_env,
)

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

# A page that hooks its own world: what the agent searches for is recorded in window.__seen, every element's innerText
# reads as the page's own words, and a querySelectorAll finds the page's choice (its <title>) whatever was asked.
HOOKED = (
    "<title>hooked</title><p id='real'>The real text</p>"
    "<script>"
    "window.__seen = [];"
    "const lower = String.prototype.toLowerCase;"
    "String.prototype.toLowerCase = function () { window.__seen.push(String(this)); return lower.call(this); };"
    "const all = Document.prototype.querySelectorAll;"
    "Document.prototype.querySelectorAll = function (selector) {"
    " window.__seen.push(String(selector)); return all.call(this, 'title'); };"
    "Object.defineProperty(HTMLElement.prototype, 'innerText', { get() { return 'PAGE-SUPPLIED TEXT'; } });"
    "</script>"
)


def _gateway(manager) -> McpToolGateway:
    manager.ensure_session_accessible = AsyncMock(return_value=None)
    return McpToolGateway(manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())


async def test_get_html_text_only_reads_the_text_the_page_shows_not_its_hooks(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/hooked": HOOKED}) as env:
        session_id = await env.create("/hooked")

        result = await _gateway(env.manager)._get_html(GetPageHtmlInput(session_id=session_id, text_only=True))

        assert "The real text" in result["content"], result
        assert "PAGE-SUPPLIED TEXT" not in result["content"]


async def test_find_elements_finds_what_is_on_the_page_and_shows_the_page_nothing_it_searched_for(
    tmp_path: Path,
) -> None:
    async with browser_env(tmp_path, {"/hooked": HOOKED}) as env:
        session_id = await env.create("/hooked")
        session = env.manager.sessions[session_id]
        gateway = _gateway(env.manager)

        by_text = await gateway._find_elements(FindElementsInput(session_id=session_id, query="Real Text"))
        by_selector = await gateway._find_elements(FindElementsInput(session_id=session_id, selector="#real"))
        seen = await session.page.evaluate("() => window.__seen")

        assert [(item["tag"], item["text"]) for item in by_text["elements"]] == [("p", "The real text")], by_text
        assert [(item["tag"], item["text"]) for item in by_selector["elements"]] == [("p", "The real text")]
        assert not any("real text" in str(item).lower() or item == "#real" for item in seen), json.dumps(seen)


async def test_an_invalid_selector_is_reported_as_one(tmp_path: Path) -> None:
    """The isolated world reports a script that threw with a hint about elements ("Observe again before acting on
    this element."), which said nothing true of a selector the agent mistyped; the script now reports it itself."""
    async with browser_env(tmp_path, {"/hooked": HOOKED}) as env:
        session_id = await env.create("/hooked")

        response = await _gateway(env.manager)._call_tool(
            McpToolCallRequest(name="browser.find_elements", arguments={"session_id": session_id, "selector": "###"})
        )

        assert response.isError and response.structuredContent["code"] == "invalid_selector", response
        assert "'###' is not a valid selector" in response.structuredContent["error"]
        assert "Observe again" not in response.content[0].text
