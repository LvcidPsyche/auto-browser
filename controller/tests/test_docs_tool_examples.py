"""Every MCP tool call shown in the repo's Markdown names a real tool with valid arguments.

examples/extract-feed-posts.md called `social.extract_posts` for months after
the tool was removed; nothing read the docs, so nothing noticed. This walks the
curl examples (`-d '{"name": ..., "arguments": ...}'`, bare or inside a JSON-RPC
`tools/call`) and validates each against the full-profile registry, and checks
that the LangChain adapter, whose description the LLM reads, names real tools.

The controller image ships only app/ and tests/, so there are no docs to read
in the Docker job; the test skips there and runs on the host and in CI.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from pydantic import ValidationError

from app.tool_gateway import ToolRegistry
from app.tool_gateway.packs import register_all

REPO_ROOT = Path(__file__).resolve().parents[2]
CURL_BODY = re.compile(r"-d '(\{.*?\})'", re.S)
QUOTED_TOOL_NAME = re.compile(r"""['"]((?:browser|harness)\.[a-z_]+)['"]""")


def _markdown_files() -> list[Path]:
    return [
        path for path in REPO_ROOT.rglob("*.md") if "node_modules" not in path.parts and path.name != "CHANGELOG.md"
    ]


def _tool_calls(text: str) -> list[dict]:
    calls = []
    for match in CURL_BODY.finditer(text):
        try:
            body = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        params = body.get("params")
        if isinstance(params, dict) and "name" in params:
            body = params
        if "name" in body and "arguments" in body:
            calls.append(body)
    return calls


@unittest.skipUnless((REPO_ROOT / "examples").is_dir(), "repo docs are not in the controller image")
class DocsToolExamplesTests(unittest.TestCase):
    def setUp(self) -> None:
        registry = ToolRegistry(tool_profile="full", experimental_enabled=lambda _: True)
        register_all(registry, MagicMock())
        self.tools = registry.tools

    def test_langchain_adapter_names_only_real_tools(self) -> None:
        # The adapter's tool description is what the LLM reads; it suggested
        # browser.navigate and browser.click, which never existed.
        package = REPO_ROOT / "integrations" / "langchain" / "auto_browser_langchain"
        names = {
            name for path in package.glob("*.py") for name in QUOTED_TOOL_NAME.findall(path.read_text(encoding="utf-8"))
        }
        self.assertTrue(names, "found no tool names in the adapter; the pattern has drifted")
        self.assertEqual(sorted(names - set(self.tools)), [])

    def test_documented_tool_calls_match_the_registry(self) -> None:
        tools = self.tools

        checked = 0
        for path in _markdown_files():
            for call in _tool_calls(path.read_text(encoding="utf-8")):
                where = f"{path.relative_to(REPO_ROOT)}: {call['name']}"
                with self.subTest(where):
                    if call["name"] not in tools:
                        self.fail(f"{where} is not a registered tool")
                    try:
                        tools[call["name"]].input_model.model_validate(call["arguments"])
                    except ValidationError as exc:
                        self.fail(f"{where} arguments do not validate: {exc}")
                checked += 1
        self.assertGreater(checked, 0, "found no documented tool calls; the pattern has drifted")
