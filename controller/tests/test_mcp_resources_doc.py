"""Guard: the MCP resources example doc stays in sync with the transport.

If a resource URI scheme or subscription method changes in code, this test
fails so the documented example (examples/mcp-resources.md) is updated too.
No browser/network — inspects the module source and the doc text.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from app.mcp_transport import McpHttpTransport

_REPO = Path(__file__).resolve().parents[2]
_DOC = _REPO / "examples" / "mcp-resources.md"
_CLIENTS_DOC = _REPO / "docs" / "mcp-clients.md"
_TRANSPORT = _REPO / "controller" / "app" / "mcp_transport.py"


class McpResourcesDocTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _DOC.exists() or not _TRANSPORT.exists():  # repo layout absent in the controller image
            self.skipTest(f"doc-sync sources unavailable: {_DOC}")
        self.doc = _DOC.read_text(encoding="utf-8")
        self.transport = _TRANSPORT.read_text(encoding="utf-8")

    def test_documented_resource_schemes_exist_in_code(self) -> None:
        for suffix in ("screenshot", "dom", "console", "network"):
            self.assertIn(f"browser://{{session_id}}/{suffix}", self.transport, suffix)
            self.assertIn(f"browser://<session-id>/{suffix}", self.doc, suffix)
        self.assertIn('"browser://sessions"', self.transport)
        self.assertIn("browser://sessions", self.doc)

    def test_documented_methods_exist_in_code(self) -> None:
        for method in (
            "resources/list",
            "resources/read",
            "resources/subscribe",
            "resources/unsubscribe",
            "notifications/resources/updated",
        ):
            self.assertIn(method, self.transport, method)
            self.assertIn(method, self.doc, method)

    def test_documented_error_codes_match(self) -> None:
        # subscribe: unknown uri -> -32002, bad params -> -32602
        self.assertIn("-32002", self.transport)
        self.assertIn("-32002", self.doc)
        self.assertIn("-32602", self.doc)

    def test_the_doc_does_not_promise_a_notification_for_every_change(self) -> None:
        clients = _CLIENTS_DOC.read_text(encoding="utf-8")
        self.assertNotIn("whenever that resource changes", self.doc)
        self.assertNotIn("When a subscribed resource changes", clients)

    def test_documented_notifications_match_the_code(self) -> None:
        uris = [f"browser://s/{kind}" for kind in ("screenshot", "dom", "console", "network")]
        for event in ("observe", "action", "session", "approval"):
            with self.subTest(event=event):
                row = re.search(rf"^\| `{event}`[^|]*\| (.+) \|$", self.doc, re.MULTILINE)
                self.assertIsNotNone(row, f"{event} is missing from the notification table")
                documented = set(re.findall(r"browser://<id>/(\w+)", row.group(1)))

                notified = McpHttpTransport._updated_resource_uris({"event": event, "session_id": "s"}, subscribed=uris)

                self.assertEqual(documented, {uri.rsplit("/", 1)[1] for uri in notified})

    def test_the_doc_says_the_audit_events_are_read_not_notified(self) -> None:
        self.assertIn("browser://audit/events", self.doc)
        self.assertIn("poll it with `resources/read`", self.doc)


if __name__ == "__main__":
    unittest.main()
