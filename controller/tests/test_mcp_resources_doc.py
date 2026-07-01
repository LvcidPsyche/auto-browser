"""Guard: the MCP resources example doc stays in sync with the transport.

If a resource URI scheme or subscription method changes in code, this test
fails so the documented example (examples/mcp-resources.md) is updated too.
No browser/network — inspects the module source and the doc text.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DOC = _REPO / "examples" / "mcp-resources.md"
_TRANSPORT = _REPO / "controller" / "app" / "mcp_transport.py"


class McpResourcesDocTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = _DOC.read_text(encoding="utf-8")
        self.transport = _TRANSPORT.read_text(encoding="utf-8")

    def test_documented_resource_schemes_exist_in_code(self) -> None:
        for suffix in ("screenshot", "dom", "console", "network"):
            self.assertIn(f'browser://{{session_id}}/{suffix}', self.transport, suffix)
            self.assertIn(f"browser://<session-id>/{suffix}", self.doc, suffix)
        self.assertIn('"browser://sessions"', self.transport)
        self.assertIn("browser://sessions", self.doc)

    def test_documented_methods_exist_in_code(self) -> None:
        for method in ("resources/list", "resources/read", "resources/subscribe",
                       "resources/unsubscribe", "notifications/resources/updated"):
            self.assertIn(method, self.transport, method)
            self.assertIn(method, self.doc, method)

    def test_documented_error_codes_match(self) -> None:
        # subscribe: unknown uri -> -32002, bad params -> -32602
        self.assertIn("-32002", self.transport)
        self.assertIn("-32002", self.doc)
        self.assertIn("-32602", self.doc)


if __name__ == "__main__":
    unittest.main()
