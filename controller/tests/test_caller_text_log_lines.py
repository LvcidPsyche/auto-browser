"""A caller's or a page's text in a log line is quoted and cut, so that it starts no line of its own.

Each of these lines wrote such a text with %s, as it came: a memory profile's name (the caller's, or an agent's that
a page steers), a page's URL, a Playwright error's text (several lines, quoting the selector or the URL the agent
gave), a mesh peer's reply, and the arguments and the error of a route's 500. Each now writes it as the route
lines beside them do (log_hidden_cause): quoted, a control character as its escape, and cut at 200 characters.
"""

from __future__ import annotations

import logging
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from playwright.async_api import Error as PlaywrightError

from app.actions.pipeline import BrowserActionPipeline
from app.browser_manager import BrowserManager
from app.memory_manager import MemoryManager
from app.mesh import delegation
from app.mesh.delegation import DelegationError, DelegationManager
from app.mesh.identity import NodeIdentity
from app.mesh.models import DelegationRequest, PeerRecord
from app.mesh.peers import PeerRegistryFile
from app.mesh.transport import make_envelope
from app.network_inspector import _scrub_body
from app.routes._utils import internal_error
from app.routes.extensions.cdp import _get_cdp
from app.utils import quoted_for_log
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage, _settings

# A line break, an ESC and 5,000 characters, after a line a reader of the log would take for the controller's.
FORGED = "p\nWARNING:app.main:forged by a caller\x1b[2J" + "x" * 5000


def _written(records: list[logging.LogRecord]) -> list[str]:
    """Each record as a handler writes it: its message, and whatever exc_info prints under it."""
    return [logging.Formatter().format(record) for record in records]


class OneLineTests(unittest.IsolatedAsyncioTestCase):
    def assert_one_line_cut(self, written: str) -> None:
        self.assertEqual(written.splitlines(), [written], ascii(written[:300]))
        self.assertIn("forged by a caller", written)
        self.assertNotIn("\x1b", written)
        # The cut mark after the closing quote (a double one where the text holds a single one): inside it, it read
        # as the text's own last characters.
        self.assertRegex(written, "xxx['\"][.][.][.]")
        self.assertLess(len(written), 600, ascii(written[:300]))

    async def test_the_memory_profile_saved_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryManager(tmp)
            # The file's name is made safe, and one of 5,000 characters is more than a file system takes.
            with (
                patch.object(memory, "_profile_path", return_value=Path(tmp) / "p.json"),
                self.assertLogs("app.memory_manager", level="INFO") as logged,
            ):
                await memory.save(FORGED, goal_summary="g")

        (written,) = _written(logged.records)
        self.assertTrue(written.startswith("memory profile saved: 'p\\n"), written[:80])
        self.assert_one_line_cut(written)

    async def test_the_memory_profile_loaded_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = BrowserManager(_settings(Path(tmp)))
            manager.audit.append = AsyncMock()
            manager._persist_session = AsyncMock()
            manager._settle = AsyncMock()
            manager._maybe_provision_session_tunnel = AsyncMock()
            manager._acquire_session_browser = AsyncMock(return_value=(FakeBrowser(FakeContext(FakePage())), None))
            manager.memory = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(to_system_prompt=lambda: "")))

            with self.assertLogs("app.browser.services.session_creation", level="INFO") as logged:
                await manager.create_session(name="fixture", memory_profile=FORGED)

        (written,) = [line for line in _written(logged.records) if "memory profile loaded" in line]
        self.assertTrue(written.startswith("memory profile loaded: 'p\\n"))
        self.assert_one_line_cut(written)

    async def test_a_pages_url_in_the_network_inspectors_warning(self) -> None:
        scrubber = Mock()
        scrubber.network_body.side_effect = RuntimeError("the scrubber failed")

        with self.assertLogs("app.network_inspector", level="WARNING") as logged:
            self.assertEqual(
                _scrub_body(scrubber, "body", "text/plain", "https://shop.example/" + FORGED), (None, True)
            )

        (written,) = _written(logged.records)
        self.assertIn("could not scrub a body for 'https://shop.example/p\\n", written)
        self.assertTrue(written.endswith(": RuntimeError"), written[-40:])
        self.assert_one_line_cut(written)

    async def test_a_playwright_errors_text_in_a_post_action_observation_line(self) -> None:
        manager = SimpleNamespace(
            _observation_payload=AsyncMock(side_effect=PlaywrightError(FORGED)),
            _settle=AsyncMock(),
            _shown_url=lambda page: "https://shop.example/",
        )
        context = SimpleNamespace(manager=manager, session=SimpleNamespace(id="s1", page=None), action_name="click")

        with self.assertLogs("app.actions.pipeline", level="INFO") as logged:
            await BrowserActionPipeline._after_observation(context)

        retrying, failed = _written(logged.records)
        self.assertIn("post-action observation failed for session s1, retrying: 'p\\n", retrying)
        self.assertIn("post-action observation failed for session s1: 'p\\n", failed)
        for written in (retrying, failed):
            self.assert_one_line_cut(written)

    async def test_a_mesh_peers_reply_in_the_reply_verification_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server, peer = NodeIdentity(Path(tmp) / "server"), NodeIdentity(Path(tmp) / "peer")
            peers = PeerRegistryFile(Path(tmp) / "peers.json")
            peers.add(PeerRecord(node_id=peer.node_id, pubkey_b64=peer.pubkey_b64))
            manager = DelegationManager(identity=server, peers=peers)
            # The peer's reply names a recipient of its own, which the verification error quotes.
            reply = make_envelope(peer, {"status": "ok"}, recipient_node_id=FORGED)

            async def send(*, peer, envelope):
                return reply

            with (
                patch.object(delegation, "send_envelope", send),
                self.assertLogs("app.mesh.delegation", level="WARNING") as logged,
                self.assertRaises(DelegationError),
            ):
                await manager.delegate(peer.node_id, DelegationRequest(capability="tool:browser.observe"))

        (written,) = _written(logged.records)
        self.assertIn(f"mesh.delegation.reply_verification_failed peer={peer.node_id} error=", written)
        self.assert_one_line_cut(written)

    async def test_a_browsers_error_in_the_cdp_routes_attach_line(self) -> None:
        # The session id is one the controller made, which cdp_sessions holds; the error is the browser's, which
        # Playwright writes over several lines (its call log, the browser's own log).
        session_id = "0123456789ab"
        active_tab = SimpleNamespace(
            context=SimpleNamespace(new_cdp_session=AsyncMock(side_effect=PlaywrightError(FORGED)))
        )
        app = SimpleNamespace(
            state=SimpleNamespace(
                cdp_sessions={session_id: SimpleNamespace(page=SimpleNamespace())},
                browser_manager=SimpleNamespace(sessions={session_id: SimpleNamespace(page=active_tab)}),
            )
        )

        with (
            self.assertLogs("app.routes.extensions.cdp", level="WARNING") as logged,
            self.assertRaises(HTTPException) as raised,
        ):
            await _get_cdp(app, session_id)

        self.assertEqual(raised.exception.status_code, 503)
        (written,) = _written(logged.records)
        self.assertTrue(
            written.startswith(f"cdp: could not attach to the active tab of {session_id}: 'p\\n"), ascii(written[:80])
        )
        self.assert_one_line_cut(written)


class CutMarkTests(unittest.TestCase):
    def test_a_quoted_text_that_is_cut_has_its_mark_after_the_closing_quote(self) -> None:
        # 'xyyy...' read as a text that ends in three dots; 'xyyy'... says the text went on.
        self.assertEqual(quoted_for_log("x" + "y" * 4999), "'x" + "y" * 199 + "'...")
        self.assertEqual(quoted_for_log("x" * 200), "'" + "x" * 200 + "'")
        self.assertEqual(quoted_for_log("a\nb"), "'a\\nb'")
        self.assertEqual(quoted_for_log(["z" * 300]), '"' + "['" + "z" * 198 + '"...')


class InternalErrorTests(unittest.TestCase):
    """A route's 500 writes the route's message, the error's class and text and where it was raised: understandable,
    with the caller's text quoted and cut. It wrote the route's arguments as they came, and the error's traceback by
    exc_info, which prints the error's text as it is."""

    def test_the_callers_text_starts_no_line_and_the_error_is_named_with_its_frames(self) -> None:
        def route_body() -> None:
            raise RuntimeError("boom for " + FORGED)

        with self.assertLogs("tests.internal_error", level="ERROR") as logged:
            try:
                route_body()
            except RuntimeError:
                error = internal_error(logging.getLogger("tests.internal_error"), "observe failed for %s", FORGED)

        self.assertEqual((error.status_code, error.detail), (500, "Internal error"))
        (written,) = _written(logged.records)
        first, *where = written.splitlines()
        self.assertTrue(first.startswith("observe failed for 'p\\n"), first[:80])
        self.assertIn(": answered 500 for RuntimeError: 'boom for p\\n", first)
        self.assertIn(f"(cut from {len('boom for ' + FORGED)} characters)", first)
        self.assertNotIn("\x1b", written)
        self.assertEqual([line for line in where if "forged" in line], [], where)
        self.assertLess(len(first), 800)
        # Every frame it was raised through, a line each: the file, the line and the function.
        self.assertTrue(where and all(re.fullmatch(r"  .+:\d+ in \S+", line) for line in where), where)
        self.assertTrue(where[-1].endswith(" in route_body"), where)
