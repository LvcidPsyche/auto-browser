"""A caller's or a page's text in a log line is quoted and cut, so that it starts no line of its own.

Each of these lines wrote such a text with %s, as it came: a memory profile's name (the caller's, or an agent's that
a page steers), a page's URL, a Playwright error's text (several lines, quoting the selector or the URL the agent
gave), a mesh peer's reply, and the arguments and the error of a route's 500. Each now writes it as the route
lines beside them do (log_hidden_cause): quoted, a control character as its escape, and cut at 200 characters.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from app.agent_jobs import AgentJobQueue
from app.browser.services.sessions import _load_failed
from app.browser_manager import BrowserManager
from app.cdp.passthrough import CDPPassthrough
from app.memory_manager import MemoryManager
from app.mesh import delegation
from app.mesh.delegation import DelegationError, DelegationManager
from app.mesh.identity import NodeIdentity
from app.mesh.models import DelegationRequest, PeerRecord
from app.mesh.peers import PeerRegistryFile
from app.mesh.transport import make_envelope
from app.network_inspector import _scrub_body, scrub_url
from app.orchestrator import BrowserOrchestrator
from app.routes._utils import internal_error
from app.routes.extensions.cdp import _get_cdp
from app.routes.extensions.network import NetworkHookRequest, network_register_hook
from app.utils import _log_background_task_exception, quoted_for_log
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage, _settings
from tests.test_orchestrator import RepeatingAdapter, StaticRegistry

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

    async def test_a_callers_selector_and_the_browsers_error_in_the_cdp_passthroughs_lines(self) -> None:
        # The selector is the caller's, as long as the request allows, and was written with %r, whole; the error is
        # the browser's, several lines when Playwright adds its call log.
        cdp = CDPPassthrough(SimpleNamespace(send=AsyncMock(side_effect=PlaywrightError(FORGED))))

        with self.assertLogs("app.cdp.passthrough", level="WARNING") as logged:
            self.assertEqual((await cdp.get_element_intelligence(FORGED))["error"], "cdp_element_intelligence_failed")
            self.assertEqual((await cdp.raw_cdp_command("DOM.getDocument", {}))["error"], "cdp_command_failed")

        read, command = [line for line in _written(logged.records) if line.startswith("cdp.")]
        self.assertTrue(read.startswith("cdp.element_intelligence error selector='p\\n"), read[:80])
        self.assertIn(": 'p\\n", read.split(" selector=", 1)[1][200:])
        self.assertTrue(command.startswith("cdp.raw_command error method='DOM.getDocument': 'p\\n"), command[:80])
        for written in (read, command):
            self.assert_one_line_cut(written)

    async def test_a_callers_pattern_and_a_pages_url_in_the_network_hook_line(self) -> None:
        # Both were written with %r, whole: the pattern the caller registered, and the URL of a request the page made.
        registered = {}
        inspector = SimpleNamespace(register_hook=lambda pattern, hook: registered.update({pattern: hook}))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(network_inspectors={"s1": inspector})))
        await network_register_hook("s1", NetworkHookRequest.model_construct(url_pattern=FORGED), request)

        with self.assertLogs("app.routes.extensions.network", level="INFO") as logged:
            await registered[FORGED]({"url": "https://shop.example/" + FORGED})

        (written,) = _written(logged.records)
        self.assertTrue(written.startswith("network.hook pattern='p\\n"), written[:80])
        self.assertIn(" matched url='https://shop.example/p\\n", written)
        self.assertEqual(written.splitlines(), [written], ascii(written[:300]))
        self.assertNotIn("\x1b", written)
        self.assertEqual(len(re.findall("xxx'[.][.][.]", written)), 2, written)
        self.assertLess(len(written), 600)

    async def test_an_error_no_one_awaits_is_written_quoted_cut_and_with_bounded_frames(self) -> None:
        # The record of a fire-and-forget task that failed, and of an agent job worker that crashed on its job, held
        # the error's traceback by exc_info, which prints its text as it is: a Playwright error's, a page's.
        async def fails() -> None:
            raise PlaywrightError(FORGED)

        task = asyncio.get_running_loop().create_task(fails(), name="a background task")
        with contextlib.suppress(PlaywrightError):
            await task
        with self.assertLogs("app.utils", level="ERROR") as logged:
            _log_background_task_exception(task)

        queue: asyncio.Queue[str] = asyncio.Queue()
        queue.put_nowait("job-1")
        jobs = SimpleNamespace(
            queue=queue,
            _process_job=AsyncMock(side_effect=PlaywrightError(FORGED)),
            _running_tasks={},
            _cancellation_reasons={},
            _finishing=set(),
        )
        with self.assertLogs("app.agent_jobs", level="ERROR") as crashed:
            worker = asyncio.get_running_loop().create_task(AgentJobQueue._worker_loop(jobs, 0))
            await queue.join()
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

        for record, starts in (
            (logged.records[0], "background task a background task failed: Error: 'p\\n"),
            (crashed.records[0], "agent job worker 0 crashed on job-1: Error: 'p\\n"),
        ):
            self.assertIsNone(record.exc_info)
            first, *where = _written([record])[0].splitlines()
            self.assertTrue(first.startswith(starts), first[:80])
            self.assert_one_line_cut(first)
            self.assertTrue(where and all(re.fullmatch(r"  .+:\d+ in \S+", line) for line in where), where)

    async def test_an_agent_step_that_failed_is_written_quoted_cut_and_with_bounded_frames(self) -> None:
        # By exc_info, a traceback that prints the error's text as it is: a Playwright error's, a page's.
        manager = SimpleNamespace(
            settings=SimpleNamespace(autonomous_approvals=False, force_governed_approvals=False),
            observe=AsyncMock(return_value={"url": "https://shop.example/", "title": "", "text_excerpt": ""}),
            execute_decision=AsyncMock(side_effect=PlaywrightError(FORGED)),
            require_governed_approval=AsyncMock(return_value=None),
            get_session=AsyncMock(return_value=SimpleNamespace(artifact_dir=Path("unused"))),
            _append_jsonl=AsyncMock(),
        )
        orchestrator = BrowserOrchestrator(manager, StaticRegistry(RepeatingAdapter()))

        with self.assertLogs("app.orchestrator", level="ERROR") as logged:
            result = await orchestrator.step(session_id="s1", provider_name="openai", goal="Click")

        self.assertEqual(result.status, "error")
        (record,) = logged.records
        self.assertIsNone(record.exc_info)
        first, *where = _written([record])[0].splitlines()
        self.assertTrue(first.startswith("agent step failed for session s1 (provider=openai): Error: 'p\\n"), first)
        self.assert_one_line_cut(first)
        self.assertTrue(where and all(re.fullmatch(r"  .+:\d+ in \S+", line) for line in where), where)

    async def test_a_pages_url_in_the_line_of_a_page_that_did_not_load(self) -> None:
        # Its first line, cut at 300 characters, but an ESC in it as it came: the URL is the caller's start_url.
        url = "https://shop.example/" + FORGED.replace("\n", "")
        error = PlaywrightError(f"Page.goto: net::ERR_ABORTED at {url}\nCall log:\n  - navigating to {url}")

        with self.assertLogs("app.browser.services.sessions", level="INFO") as logged:
            _load_failed(error, action="create_session")

        (written,) = _written(logged.records)
        self.assertTrue(written.startswith("create_session: the page did not load: 'Page.goto: net::"), written[:80])
        self.assert_one_line_cut(written)

    async def test_a_pages_origin_in_the_line_of_a_url_whose_query_could_not_be_scrubbed(self) -> None:
        # Its origin alone, but as long as the page wrote its host, and an ESC in it as it came (urlsplit drops a
        # line break, and refuses a host with a bracket in it).
        scrubber = SimpleNamespace(url=Mock(side_effect=RuntimeError("the scrubber failed")))
        host = "shop.example.forged by a caller\x1b" + "x" * 5000

        with self.assertLogs("app.network_inspector", level="WARNING") as logged:
            scrub_url(f"https://{host}/?q=1", scrubber, 1024)

        (written,) = _written(logged.records)
        self.assertTrue(written.startswith("network inspector could not scrub the query of 'https://shop."), written)
        self.assertTrue(written.endswith(": RuntimeError"), written[-40:])
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

    def _record(self, raise_it) -> str:
        """The record internal_error writes for what ``raise_it`` raises, caught here as a route catches it."""
        with self.assertLogs("tests.internal_error", level="ERROR") as logged:
            try:
                raise_it()
            except Exception:
                error = internal_error(logging.getLogger("tests.internal_error"), "observe failed for %s", "s1")
        self.assertEqual((error.status_code, error.detail), (500, "Internal error"))
        (written,) = _written(logged.records)
        return written

    def test_a_deep_errors_record_is_as_long_whatever_its_depth(self) -> None:
        # A RecursionError's record held a line for each of its thousand frames: 118,954 characters. The depth is a
        # function that calls itself as deep as it is asked, which no recursion limit decides.
        def dive(depth: int) -> None:
            if depth == 0:
                raise RecursionError("maximum recursion depth exceeded")
            dive(depth - 1)

        records = {depth: self._record(lambda: dive(depth)) for depth in (40, 400)}

        for depth, written in records.items():
            first, *where = written.splitlines()
            self.assertEqual(
                first, "observe failed for 's1': answered 500 for RecursionError: 'maximum recursion depth exceeded'"
            )
            # The route's own frames (here _record's, the lambda's, the first dive), the count of those between, and
            # the ten nearest the raise: 2 + depth + 1 frames in all.
            frames = [line for line in where if re.fullmatch(r"  .+:\d+ in \S+", line)]
            self.assertEqual(len(frames), 13, where)
            self.assertEqual(where[3], f"  ... {depth + 3 - 13} frames left out", where)
            self.assertEqual([line.rsplit(" in ", 1)[1] for line in where[:3]], ["_record", "<lambda>", "dive"])
            self.assertTrue(all(line.endswith(" in dive") for line in where[4:]), where)
            self.assertEqual(len(where), 14)
        # Ten times as deep, and one digit longer: the count's.
        self.assertEqual(len(records[400]) - len(records[40]), 1)
        self.assertLess(len(records[400]), 3000)

    def test_each_error_it_was_raised_from_or_while_handling_is_named(self) -> None:
        def raised_from() -> None:
            try:
                raise ValueError("inner " + FORGED)
            except ValueError as exc:
                raise RuntimeError("outer") from exc

        def raised_while_handling() -> None:
            try:
                raise KeyError("first")
            except KeyError:
                raise RuntimeError("second")

        def raised_from_none() -> None:
            try:
                raise KeyError("hidden")
            except KeyError:
                raise RuntimeError("alone") from None

        def raised_through_five() -> None:
            error: BaseException | None = None
            for index in range(5):
                try:
                    raise OSError(f"cause {index}") from error
                except OSError as exc:
                    error = exc
            raise RuntimeError("last") from error

        written = self._record(raised_from)
        (chained,) = [line for line in written.splitlines() if line.startswith("  raised ")]
        # On its own line, by its class and its text, quoted and cut: the caller's text in it starts no line.
        self.assertEqual([line for line in written.splitlines() if "forged by a caller" in line], [chained])
        self.assertTrue(chained.startswith("  raised from ValueError: 'inner p\\n"), chained[:80])
        self.assertIn(f"'... (cut from {len('inner ' + FORGED)} characters)", chained)
        self.assertNotIn("\x1b", written)
        self.assertLess(len(chained), 400)

        written = self._record(raised_while_handling)
        self.assertIn("\n  raised while handling KeyError: \"'first'\"", written)

        written = self._record(raised_from_none)
        self.assertNotIn("hidden", written)
        self.assertNotIn("  raised ", written)

        # The nearest three, and how many more: a chain can be as long as its errors.
        lines = self._record(raised_through_five).splitlines()
        self.assertEqual(
            [line for line in lines if line.startswith(("  raised ", "  and "))],
            [
                "  raised from OSError: 'cause 4'",
                "  raised from OSError: 'cause 3'",
                "  raised from OSError: 'cause 2'",
                "  and 2 more before them",
            ],
        )
