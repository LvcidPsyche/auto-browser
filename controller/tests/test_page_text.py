"""Page text keeps its structure, and browser.get_html is bounded and paged.

The observation's text_excerpt squashed every run of whitespace to one space,
so a table's cells, a list's items and separate paragraphs ran together into
one line. browser.get_html returned the whole serialized DOM or text in one
result, routinely megabytes on a real page, which overran the context of the
model that asked for it.
"""

from __future__ import annotations

import asyncio
import glob
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.browser_scripts import PAGE_SUMMARY_SCRIPT, PAGE_TEXT_SCRIPT
from app.models import McpToolCallRequest
from app.tool_gateway import McpToolGateway

NODE = shutil.which("node")

RAW_INNER_TEXT = (
    "Orders\r\n\r\n\r\n\r\nID\tCustomer \t Total\n1001\tJane   Doe\t$120.00\n  - first item  \n- second\u00a0item\n\n"
)
EXPECTED_TEXT = "Orders\n\nID\tCustomer\tTotal\n1001\tJane Doe\t$120.00\n- first item\n- second item"


def _page_text_under_node(inner_text: str) -> str:
    source = (
        f"const document = {{ body: {{ innerText: {json.dumps(inner_text)} }} }};\n"
        f"console.log(JSON.stringify(({PAGE_TEXT_SCRIPT})()));\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(source)
    try:
        completed = subprocess.run([NODE, handle.name], capture_output=True, text=True, timeout=30, check=True)
    finally:
        Path(handle.name).unlink(missing_ok=True)
    return json.loads(completed.stdout)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_lines_and_cells_survive_normalisation() -> None:
    assert _page_text_under_node(RAW_INNER_TEXT) == EXPECTED_TEXT


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_an_empty_body_is_empty_text() -> None:
    assert _page_text_under_node("") == ""


def test_the_excerpt_and_get_html_share_one_normaliser() -> None:
    assert "const readable" in PAGE_TEXT_SCRIPT
    assert "const readable" in PAGE_SUMMARY_SCRIPT
    assert "__READABLE_TEXT__" not in PAGE_SUMMARY_SCRIPT
    assert "readable(document.body?.innerText).slice(0, textLimit)" in PAGE_SUMMARY_SCRIPT


def _chromium() -> str | None:
    candidates = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return candidates[-1] if candidates else None


@pytest.mark.skipif(_chromium() is None, reason="no local Chromium binary")
def test_real_chromium_excerpt_keeps_a_table_readable() -> None:
    from playwright.async_api import async_playwright

    html = """
      <h1>Orders</h1>
      <table><tr><th>ID</th><th>Total</th></tr><tr><td>1001</td><td>$120.00</td></tr></table>
      <ul><li>Shipped</li><li>Pending</li></ul>
    """

    async def run() -> tuple[str, str]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=_chromium())
            try:
                page = await browser.new_page()
                await page.set_content(html)
                summary = await page.evaluate(PAGE_SUMMARY_SCRIPT, 2000)
                return summary["text_excerpt"], await page.evaluate(PAGE_TEXT_SCRIPT)
            finally:
                await browser.close()

    excerpt, text = asyncio.run(run())

    assert excerpt == text
    assert "ID\tTotal\n1001\t$120.00" in text
    assert "Shipped\nPending" in text


class GetHtmlPagingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.html = "<html>" + "x" * 2_494 + "</html>"  # 2,507 characters
        self.text = "line\n" * 500  # 2,500 characters
        self.page = SimpleNamespace(
            content=AsyncMock(side_effect=lambda: self.html),
            evaluate=AsyncMock(side_effect=self._evaluate),
        )
        self.manager = SimpleNamespace(
            get_session=AsyncMock(return_value=SimpleNamespace(page=self.page)),
            require_governed_approval=AsyncMock(return_value=None),
        )
        self.gateway = McpToolGateway(
            manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
        )

    async def _evaluate(self, script: str, *args: Any) -> str:
        assert script is PAGE_TEXT_SCRIPT, "text_only must use the shared normaliser"
        return self.text

    async def _get_html(self, **arguments: Any) -> dict[str, Any]:
        response = await self.gateway.call_tool(
            McpToolCallRequest(name="browser.get_html", arguments={"session_id": "sess-1", **arguments})
        )
        self.assertFalse(response.isError, response.content[0].text)
        return response.structuredContent

    async def test_pages_cover_the_document_exactly_once(self) -> None:
        for text_only, document in ((False, self.html), (True, self.text)):
            with self.subTest(text_only=text_only):
                chunks, offset = [], 0
                while offset is not None:
                    page = await self._get_html(text_only=text_only, offset=offset, max_chars=1_000)
                    self.assertEqual(page["total_chars"], len(document))
                    self.assertEqual(page["type"], "text" if text_only else "html")
                    self.assertEqual(page["truncated"], page["next_offset"] is not None)
                    chunks.append(page["content"])
                    offset = page["next_offset"]
                self.assertEqual("".join(chunks), document)
                self.assertEqual([len(chunk) for chunk in chunks], [1_000, 1_000, len(document) - 2_000])

    async def test_the_default_bound_applies(self) -> None:
        self.html = "y" * 50_000

        page = await self._get_html()

        self.assertEqual(len(page["content"]), 20_000)
        self.assertEqual(page["next_offset"], 20_000)
        self.assertTrue(page["truncated"])

    async def test_a_short_page_is_returned_whole(self) -> None:
        page = await self._get_html(text_only=True, max_chars=5_000)

        self.assertEqual(page["content"], self.text)
        self.assertFalse(page["truncated"])
        self.assertIsNone(page["next_offset"])

    async def test_an_offset_past_the_end_is_empty_not_an_error(self) -> None:
        page = await self._get_html(offset=10_000)

        self.assertEqual(page["content"], "")
        self.assertFalse(page["truncated"])

    async def test_deprecated_full_page_is_accepted_but_not_advertised(self) -> None:
        # It never did anything; callers that still send it keep working, but
        # the schema every tools/list carries no longer spends space on it.
        page = await self._get_html(full_page=True, max_chars=1_000)
        schema = next(tool for tool in self.gateway.list_tools() if tool["name"] == "browser.get_html")

        self.assertEqual(page["type"], "html")
        self.assertNotIn("full_page", schema["inputSchema"]["properties"])
        self.assertNotIn("full_page", schema["description"])

    async def test_bounds_are_validated(self) -> None:
        for arguments in ({"max_chars": 10}, {"max_chars": 2_000_000}, {"offset": -1}):
            with self.subTest(**arguments):
                response = await self.gateway.call_tool(
                    McpToolCallRequest(name="browser.get_html", arguments={"session_id": "sess-1", **arguments})
                )
                self.assertTrue(response.isError)
