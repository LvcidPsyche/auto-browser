"""The model never receives an isolated session's noVNC password (NAV-14).

Each docker_ephemeral session's noVNC has its own random password. The handoff results (create_session,
get_session, get_remote_access, request_human_takeover, over REST and MCP) used to carry it in the takeover URL's
fragment, and over MCP those results reach the calling model; with a session tunnel up that URL is the public one.
The full link now comes only from GET /sessions/{session_id}/takeover-link, which the dashboard and
scripts/takeover_link.sh read and no MCP tool or agent-facing package exposes.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as main_module
from app import session_isolation
from app.audit import get_current_operator
from app.browser_manager import BrowserManager
from app.main import handle_key_not_found
from app.models import McpToolCallRequest, OperatorIdentity
from app.routes.extensions import _DASHBOARD_HTML
from app.routes.operations import create_operations_router
from app.routes.sessions import (
    TAKEOVER_LINK_REFUSED_DETAIL,
    HeldBack,
    RefusedCall,
    TakeoverRefusalWindows,
    create_sessions_router,
    write_held_takeover_refusals,
)
from app.session_isolation import DockerBrowserNodeProvisioner
from app.tool_gateway.gateway import McpToolGateway
from tests._bash import BASH, NO_BASH_REASON
from tests.test_auth_state_ownership import StateContext
from tests.test_browser_manager_create_session import FakeBrowser, FakePage
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer
from tests.test_session_vnc_password import _settings

SENTINEL = "Snt9Pw7Q"
TAKEOVER = "takeover-sentinel-0123456789abcdefghijklmnop"
REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "takeover_link.sh"
# The tests that run the script itself give it /usr/bin and /bin's tools on PATH, which Windows does not have.
POSIX_SCRIPT_REASON = "scripts/ is not here, the test " + NO_BASH_REASON + ", or this is Windows (no /usr/bin)"
_PAGE_SUMMARY = {
    "title": "Fixture Page",
    "active_element": None,
    "text_excerpt": "",
    "dom_outline": {},
    "accessibility_outline": {"available": False, "nodes": []},
}


class _Isolated(unittest.IsolatedAsyncioTestCase):
    """A docker_ephemeral manager whose sessions are provisioned on a fake Docker, with the password a sentinel."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        settings = _settings(root, SESSION_ISOLATION_MODE="docker_ephemeral", ISOLATED_HOST_DATA_ROOT=str(root))
        settings.max_sessions = 5
        settings.takeover_token = TAKEOVER
        patcher = patch.object(session_isolation, "_new_vnc_password", return_value=SENTINEL)
        patcher.start()
        self.addCleanup(patcher.stop)
        controller = FakeDockerContainer(container_id="controller-id", name="controller", attrs={})
        provisioner = DockerBrowserNodeProvisioner(settings, client=FakeDockerClient(controller))
        provisioner._controller_container_id = controller.id
        manager = BrowserManager(settings)
        manager.runtime_provisioner = provisioner
        manager._settle = AsyncMock()  # type: ignore[method-assign]
        manager._maybe_provision_session_tunnel = AsyncMock()  # type: ignore[method-assign]

        async def acquire(session_id: str):
            browser = FakeBrowser(StateContext(FakePage()))
            browser.new_context = AsyncMock(side_effect=lambda **_kwargs: StateContext(FakePage(), marker="own"))
            return browser, await provisioner.provision(session_id)

        manager._acquire_session_browser = acquire  # type: ignore[method-assign]
        # FakePage has no screenshot, so the default preset's observe fails, and a sentinel check on its error
        # passes whatever the result would carry. The text preset reads only these two, so observe succeeds.
        manager.element_ids.evaluate = AsyncMock(return_value=[])  # type: ignore[method-assign]
        manager.observation.page_summary = AsyncMock(return_value=_PAGE_SUMMARY)  # type: ignore[method-assign]
        self.manager = manager
        self.audit = AsyncMock(wraps=manager.audit.append)
        manager.audit.append = self.audit  # type: ignore[method-assign]
        self.gateway = McpToolGateway(manager=manager, orchestrator=Mock(), job_queue=Mock(), tool_profile="full")
        app = FastAPI()
        app.add_exception_handler(KeyError, handle_key_not_found)  # the controller's own: an unknown session is 404
        self.refusal_windows = TakeoverRefusalWindows()
        app.include_router(create_sessions_router(manager=manager, refusal_windows=self.refusal_windows))
        app.include_router(create_operations_router(manager=manager, proxy_store=Mock(), cron_service=Mock()))
        self.app = app
        self.client = TestClient(app)

    async def call(self, tool: str, **arguments) -> str:
        response = await self.gateway.call_tool(McpToolCallRequest(name=tool, arguments=arguments))
        dumped = response.model_dump_json()
        self.assertFalse(response.isError, dumped)
        return dumped

    def link(self, session_id: str, token: str | None = TAKEOVER):
        headers = {} if token is None else {"X-Takeover-Token": token}
        return self.client.get(f"/sessions/{session_id}/takeover-link", headers=headers)


class NoPasswordInAgentResultsTests(_Isolated):
    async def test_no_mcp_result_carries_the_password(self) -> None:
        created = await self.call("browser.create_session", name="iso")
        self.assertNotIn(SENTINEL, created)
        session_id = next(iter(self.manager.sessions))
        session = self.manager.sessions[session_id]
        self.assertEqual(session.runtime.vnc_password, SENTINEL)  # the session has one; no result shows it

        for name, arguments in (
            ("browser.get_session", {"session_id": session_id}),
            ("browser.get_remote_access", {"session_id": session_id}),
            ("browser.request_human_takeover", {"session_id": session_id, "reason": "log in"}),
            ("browser.fork_session", {"session_id": session_id}),
            ("browser.list_sessions", {}),
            ("browser.observe", {"session_id": session_id, "preset": "text"}),
        ):
            with self.subTest(tool=name):
                self.assertNotIn(SENTINEL, await self.call(name, **arguments))

    async def test_the_takeover_result_says_how_the_person_opens_the_full_link(self) -> None:
        await self.call("browser.create_session", name="iso")
        session_id = next(iter(self.manager.sessions))

        result = await self.manager.request_human_takeover(session_id, "log in")

        # W2-26: the note names no runnable script, only the operator's path.
        note = (
            "This link has no noVNC password. The operator opens the full link from the dashboard; otherwise noVNC "
            "asks for the password."
        )
        self.assertEqual(result["takeover_note"], note)
        self.assertEqual(result["remote_access"]["takeover_note"], note)
        self.assertNotIn("#password=", result["takeover_url"])

    async def test_no_rest_response_carries_the_password(self) -> None:
        created = self.client.post("/sessions", json={"name": "iso"})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertNotIn(SENTINEL, created.text)
        session_id = created.json()["id"]

        for method, path, body in (
            ("get", f"/sessions/{session_id}", None),
            ("get", f"/remote-access?session_id={session_id}", None),
            ("post", f"/sessions/{session_id}/takeover", {"reason": "log in"}),
            ("get", "/sessions", None),
            ("get", f"/sessions/{session_id}/observe?preset=text", None),
            ("post", f"/sessions/{session_id}/fork", None),
        ):
            with self.subTest(path=path):
                kwargs = {"json": body} if body is not None else {}
                response = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(SENTINEL, response.text)


class TakeoverLinkEndpointTests(_Isolated):
    async def test_the_endpoint_returns_the_full_link_uncached_and_audits_without_it(self) -> None:
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        self.audit.reset_mock()

        response = self.link(session_id)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        self.assertEqual(body["session_id"], session_id)
        self.assertTrue(body["takeover_url"].endswith(f"#password={SENTINEL}"), body)
        viewed = [c for c in self.audit.await_args_list if c.kwargs.get("event_type") == "takeover_link_viewed"]
        self.assertEqual(len(viewed), 1)
        self.assertNotIn(SENTINEL, repr(viewed[0]))
        self.assertNotIn("vnc.html", repr(viewed[0]))

    async def test_an_unknown_or_closed_session_is_404(self) -> None:
        unknown = self.link("no-such-session")
        self.assertEqual(unknown.status_code, 404, unknown.text)
        self.assertEqual(unknown.json().get("code"), "unknown_session", unknown.text)
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        await self.manager.close_session(session_id)

        closed = self.link(session_id)
        self.assertEqual(closed.status_code, 404, closed.text)
        self.assertEqual(closed.json().get("code"), "session_closed", closed.text)

    async def test_a_shared_browser_node_session_gets_its_plain_url(self) -> None:
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        self.manager.sessions[session_id].isolation_mode = "shared_browser_node"

        response = self.link(session_id)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("#password=", response.json()["takeover_url"])

    async def test_the_endpoint_is_no_mcp_tool(self) -> None:
        names = {tool["name"] for tool in self.gateway.list_tools()}
        self.assertFalse(any("takeover_link" in name or "takeover-link" in name for name in names), names)


class TakeoverTokenTests(_Isolated):
    """The endpoint also demands X-Takeover-Token, a secret the person holds and agents are not given (NAV-14b)."""

    def refusals(self) -> list:
        return [c for c in self.audit.await_args_list if c.kwargs.get("event_type") == "takeover_link_refused"]

    @patch("app.routes.sessions.TAKEOVER_REFUSAL_WINDOW_SECONDS", 0)  # every refusal audits: this reads each event
    async def test_a_missing_or_wrong_token_is_403_and_audited_without_the_token_or_link(self) -> None:
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        self.audit.reset_mock()
        for token in (None, "", "wrong-" + TAKEOVER, TAKEOVER[:-1]):
            with self.subTest(token=token):
                response = self.link(session_id, token=token)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertNotIn(SENTINEL, response.text)
                self.assertEqual(response.json()["detail"], self.link("other", token=None).json()["detail"])
        refusals = self.refusals()
        self.assertGreaterEqual(len(refusals), 4)
        for refusal in refusals:
            self.assertNotIn(TAKEOVER, repr(refusal))
            self.assertNotIn(TAKEOVER[:-1], repr(refusal))
            self.assertNotIn(SENTINEL, repr(refusal))
            self.assertNotIn("vnc.html", repr(refusal))
        viewed = [c for c in self.audit.await_args_list if c.kwargs.get("event_type") == "takeover_link_viewed"]
        self.assertEqual(viewed, [])

    async def test_the_right_token_is_200(self) -> None:
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        response = self.link(session_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["takeover_url"].endswith(f"#password={SENTINEL}"))

    async def test_an_unknown_session_without_the_token_is_403_not_404(self) -> None:
        response = self.link("no-such-session", token=None)
        self.assertEqual(response.status_code, 403, response.text)

    async def test_no_loaded_token_is_503_with_a_fixed_detail(self) -> None:
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        self.manager.settings.takeover_token = None
        first = self.link(session_id, token=None)
        second = self.link(session_id, token="anything")
        self.assertEqual(first.status_code, 503, first.text)
        self.assertEqual(second.status_code, 503, second.text)
        self.assertEqual(first.json()["detail"], second.json()["detail"])
        self.assertNotIn(SENTINEL, first.text)
        # W4-18: a file with an unusable token, an API credential or a mode that cannot be tightened is not fixed by
        # making it writable, so the text points at the startup log, which names the cause.
        self.assertEqual(
            first.json()["detail"],
            "The controller has no takeover token: set TAKEOVER_TOKEN, or fix TAKEOVER_TOKEN_FILE as the startup log "
            "says (it names the cause), and restart.",
        )

    async def test_tokenless_loopback_still_needs_the_takeover_token(self) -> None:
        from types import SimpleNamespace

        from app.middleware.http import install_controller_http_middleware

        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        settings = self.manager.settings
        settings.api_bind_scope = "loopback"
        settings.api_allow_tokenless_loopback = True
        settings.api_bearer_token = None
        app = FastAPI()
        app.add_exception_handler(KeyError, handle_key_not_found)
        app.include_router(create_sessions_router(manager=self.manager))
        install_controller_http_middleware(
            app, settings=settings, rate_limiter=None, metrics=SimpleNamespace(enabled=False)
        )
        client = TestClient(app)

        self.assertEqual(client.get("/sessions").status_code, 200)  # tokenless loopback really is on
        refused = client.get(f"/sessions/{session_id}/takeover-link")
        self.assertEqual(refused.status_code, 403, refused.text)
        allowed = client.get(f"/sessions/{session_id}/takeover-link", headers={"X-Takeover-Token": TAKEOVER})
        self.assertEqual(allowed.status_code, 200, allowed.text)


NOBODY = OperatorIdentity(id="anonymous", source="anonymous")
NONE_HELD = HeldBack(0, False)


def refused_call(session_id: str = "s1", reason: str = "wrong takeover token") -> RefusedCall:
    return RefusedCall(session_id=session_id, reason=reason, operator=NOBODY)


class TakeoverRefusalWindowsEventNotWrittenTests(unittest.TestCase):
    SOURCE = "host:203.0.113.9"

    def setUp(self) -> None:
        self.windows = TakeoverRefusalWindows()

    def record(self, now: float, source: str = SOURCE) -> HeldBack | None:
        return self.windows.record(source, refused_call(), now=now)

    def test_a_window_whose_event_was_not_written_does_not_stand(self) -> None:
        self.assertEqual(self.record(1000.0), NONE_HELD)

        self.windows.event_not_written(self.SOURCE, refused_call(), NONE_HELD)

        self.assertEqual(self.record(1001.0), HeldBack(1, False))  # the refusal whose event was lost
        self.assertIsNone(self.record(1002.0))  # that event opened the window

    def test_what_the_lost_event_was_to_report_is_kept_with_this_refusal(self) -> None:
        self.record(1000.0)
        self.record(1001.0)
        reported = self.record(1060.0)
        self.assertEqual(reported, HeldBack(1, False))

        self.windows.event_not_written(self.SOURCE, refused_call(), reported)

        self.assertEqual(self.record(1061.0), HeldBack(2, False))

    def test_counts_added_while_the_append_failed_are_kept(self) -> None:
        self.assertEqual(self.record(1000.0), NONE_HELD)
        self.assertIsNone(self.record(1000.5))  # concurrent, during the append

        self.windows.event_not_written(self.SOURCE, refused_call(), NONE_HELD)

        self.assertEqual(self.record(1001.0), HeldBack(2, False))

    def test_a_source_pushed_out_meanwhile_audits_as_new(self) -> None:
        self.windows.event_not_written("host:never-seen", refused_call(), NONE_HELD)  # nothing to undo, and no error

        self.assertEqual(self.record(1000.0, "host:never-seen"), NONE_HELD)


class TakeoverRefusalWindowsHeldTests(unittest.TestCase):
    """What was only counted can be taken to be written without waiting for the source's next refusal."""

    SOURCE = "host:203.0.113.9"

    def setUp(self) -> None:
        self.windows = TakeoverRefusalWindows()

    def refuse(self, now: float, session_id: str = "s1", source: str = SOURCE, reason: str = "wrong takeover token"):
        return self.windows.record(source, refused_call(session_id, reason), now=now)

    def test_nothing_is_taken_while_the_window_runs_and_all_of_it_once_it_is_over(self) -> None:
        for second in range(5):
            self.refuse(1000.0 + second)

        self.assertEqual(self.windows.take_held(now=1059.9), [])
        taken = self.windows.take_held(now=1060.0)

        # The event is the last counted refusal's, and it reports the three counted before it: with the first
        # refusal's own event, five.
        self.assertEqual(taken, [(self.SOURCE, refused_call(), HeldBack(3, False))])
        self.assertEqual(self.windows.take_held(now=1061.0), [])  # taken once

    def test_one_counted_refusal_is_its_own_event_with_no_count(self) -> None:
        self.refuse(1000.0)
        self.refuse(1001.0, reason="missing takeover token")

        taken = self.windows.take_held(now=1060.0)

        self.assertEqual(taken, [(self.SOURCE, refused_call(reason="missing takeover token"), NONE_HELD)])

    def test_a_window_with_nothing_counted_gives_nothing(self) -> None:
        self.refuse(1000.0)

        self.assertEqual(self.windows.take_held(now=2000.0), [])
        self.assertEqual(self.windows.take_held(), [])

    def test_with_no_time_every_window_is_taken_running_or_not(self) -> None:
        for source in ("host:a", "host:b"):
            self.refuse(1000.0, source=source)
            self.refuse(1001.0, source=source)

        self.assertEqual([source for source, _call, _held in self.windows.take_held()], ["host:a", "host:b"])

    def test_one_source_is_taken_while_its_window_runs_and_the_window_stands(self) -> None:
        self.refuse(1000.0)
        self.refuse(1001.0)
        self.refuse(1002.0, source="host:other")
        self.refuse(1003.0, source="host:other")

        taken = self.windows.take_held(source=self.SOURCE)

        self.assertEqual(taken, [(self.SOURCE, refused_call(), NONE_HELD)])
        self.assertIsNone(self.refuse(1004.0))  # still inside the minute: counted again
        self.assertEqual(self.windows.take_held(source="host:never-seen"), [])

    def test_the_refusal_after_a_window_that_was_taken_reports_nothing_held(self) -> None:
        self.refuse(1000.0)
        self.refuse(1001.0)
        self.windows.take_held(now=1060.0)

        self.assertEqual(self.refuse(1061.0), NONE_HELD)

    def test_counted_refusals_that_named_another_session_are_said_to(self) -> None:
        self.refuse(1000.0, "s1")
        self.refuse(1001.0, "s2")
        self.refuse(1002.0, "s3")

        ((_source, call, held),) = self.windows.take_held(now=1060.0)

        self.assertEqual((call.session_id, held), ("s3", HeldBack(1, True)))

    def test_the_next_refusals_event_says_when_what_it_counts_named_another_session(self) -> None:
        self.refuse(1000.0, "s1")
        self.refuse(1001.0, "s2")
        self.assertEqual(self.refuse(1060.0, "s1"), HeldBack(1, True))  # the one counted named s2, the event s1

        self.refuse(1061.0, "s1")
        self.assertEqual(self.refuse(1120.0, "s1"), HeldBack(1, False))  # all of them named s1

    def test_what_a_failed_write_was_to_report_is_taken_at_the_next_try(self) -> None:
        self.refuse(1000.0, "s1")
        self.refuse(1001.0, "s2")
        self.refuse(1002.0, "s2")
        ((source, call, held),) = self.windows.take_held(source=self.SOURCE)  # while the window runs

        self.windows.event_not_written(source, call, held)

        # Held again, and the window no longer stands: the next try takes it whatever the time.
        self.assertEqual(self.windows.take_held(now=1003.0), [(self.SOURCE, refused_call("s2"), HeldBack(1, False))])

    def test_a_refusal_counted_while_a_write_failed_is_the_event_of_the_next_try(self) -> None:
        self.refuse(1000.0, "s1")
        self.refuse(1001.0, "s1")
        ((source, call, held),) = self.windows.take_held(source=self.SOURCE)
        self.refuse(1002.0, "s9")  # during the write

        self.windows.event_not_written(source, call, held)

        self.assertEqual(self.windows.take_held(now=1003.0), [(self.SOURCE, refused_call("s9"), HeldBack(1, True))])

    @patch("app.routes.sessions.TAKEOVER_REFUSAL_SOURCES_MAX", 3)
    def test_the_map_keeps_its_bound_with_refusals_held_and_taken(self) -> None:
        for index in range(10):
            self.refuse(1000.0 + index, source=f"host:{index}")
            self.refuse(1000.5 + index, source=f"host:{index}")
            self.assertLessEqual(len(self.windows._windows), 3)

        taken = self.windows.take_held(now=2000.0)

        self.assertEqual([source for source, _call, _held in taken], ["host:7", "host:8", "host:9"])
        self.assertEqual(len(self.windows._windows), 3)

    def test_a_named_sources_window_that_still_runs_is_left_when_a_time_is_given(self) -> None:
        # The sweep takes one source at a time: one that opened a new window since the sweep began is not its to take.
        self.refuse(1000.0)
        self.refuse(1001.0)

        self.assertEqual(self.windows.take_held(now=1059.0, source=self.SOURCE), [])
        self.assertEqual(
            self.windows.take_held(now=1060.0, source=self.SOURCE), [(self.SOURCE, refused_call(), NONE_HELD)]
        )

    def test_the_sources_that_hold_something_are_named_and_nothing_is_taken(self) -> None:
        self.refuse(1000.0, source="host:quiet")  # its first refusal's event is written: nothing held
        for source, started in (("host:over", 1000.0), ("host:running", 1030.0)):
            self.refuse(started, source=source)
            self.refuse(started + 1, source=source)

        self.assertEqual(self.windows.sources_holding(now=1060.0), ["host:over"])
        self.assertEqual(self.windows.sources_holding(), ["host:over", "host:running"])
        self.assertEqual(len(self.windows.take_held()), 2)


class ASweepCancelledInAWriteTests(unittest.IsolatedAsyncioTestCase):
    """A sweep cancelled while it writes one source's event leaves the sources it had not come to as they were.

    maintenance.shutdown() cancels a sweep that is under way, and the shutdown's own drain then writes what is held:
    taken from every window before the first write, the counts of the sources not yet written were gone by then.
    """

    def setUp(self) -> None:
        self.windows = TakeoverRefusalWindows()
        for source in ("host:a", "host:b"):
            for _ in range(4):  # the first refusal's event, and three that are only counted
                self.windows.record(source, refused_call(f"s-{source[-1]}"), now=1000.0)
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.written: list[tuple[str | None, dict]] = []
        self.manager = SimpleNamespace(audit=SimpleNamespace(append=self.append))
        patcher = patch("app.routes.sessions.time", Mock(monotonic=lambda: 2000.0))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def append(self, **event) -> None:
        if not self.entered.is_set():
            self.entered.set()
            await self.release.wait()  # the first append is held: until its caller is cancelled, or a test lets it go
        self.written.append((event["session_id"], event["details"]))

    def held(self) -> dict[str, int]:
        return {source: window.counted for source, window in self.windows._windows.items()}

    async def cancelled_in_its_first_append(self, *, finished_only: bool) -> None:
        sweep = asyncio.ensure_future(
            write_held_takeover_refusals(self.manager, self.windows, finished_only=finished_only)
        )
        await asyncio.wait_for(self.entered.wait(), timeout=5)
        sweep.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await sweep

    async def test_every_source_still_holds_what_it_held_and_the_shutdowns_drain_writes_it(self) -> None:
        await self.cancelled_in_its_first_append(finished_only=True)  # the maintenance sweep

        self.assertEqual(self.written, [])
        self.assertEqual(self.held(), {"host:a": 3, "host:b": 3})

        await write_held_takeover_refusals(self.manager, self.windows)  # the shutdown's own drain

        counted = {"reason": "wrong takeover token", "count": 2}
        self.assertEqual(self.written, [("s-a", counted), ("s-b", counted)])
        self.assertEqual(self.held(), {"host:a": 0, "host:b": 0})

    async def test_a_drain_of_every_window_that_is_cancelled_loses_nothing_either(self) -> None:
        await self.cancelled_in_its_first_append(finished_only=False)

        self.assertEqual(self.held(), {"host:a": 3, "host:b": 3})

        await write_held_takeover_refusals(self.manager, self.windows, finished_only=True)

        self.assertEqual([details.get("count") for _session, details in self.written], [2, 2])

    async def test_a_source_that_opened_a_new_window_during_the_sweep_is_left_to_that_window(self) -> None:
        sweep = asyncio.ensure_future(write_held_takeover_refusals(self.manager, self.windows, finished_only=True))
        await asyncio.wait_for(self.entered.wait(), timeout=5)
        # While host:a's event is written, host:b is refused again: that refusal's own event reports the three, and
        # one more is counted in the window it opened, which is not over.
        self.assertEqual(self.windows.record("host:b", refused_call("s-b"), now=2000.5), HeldBack(3, False))
        self.assertIsNone(self.windows.record("host:b", refused_call("s-b"), now=2000.6))
        self.release.set()

        await sweep

        self.assertEqual(self.written, [("s-a", {"reason": "wrong takeover token", "count": 2})])
        self.assertEqual(self.held(), {"host:a": 0, "host:b": 1})


class TakeoverRefusalWindowTests(_Isolated):
    """A flood of refusals writes one audit event per source per minute, and the next one says how many (R5C-6)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        self.audit.reset_mock()
        self.now = 1000.0
        # The module's own name for the clock: patching time.monotonic itself would stop the event loop's.
        patcher = patch("app.routes.sessions.time", Mock(monotonic=lambda: self.now))
        patcher.start()
        self.addCleanup(patcher.stop)

    def refused(self) -> list[dict]:
        calls = self.audit.await_args_list
        return [c.kwargs["details"] for c in calls if c.kwargs.get("event_type") == "takeover_link_refused"]

    def refuse(self, client: TestClient | None = None, token: str | None = "wrong") -> None:
        headers = {} if token is None else {"X-Takeover-Token": token}
        response = (client or self.client).get(f"/sessions/{self.session_id}/takeover-link", headers=headers)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["detail"], TAKEOVER_LINK_REFUSED_DETAIL)

    def from_host(self, host: str) -> TestClient:
        return TestClient(self.app, client=(host, 50000))

    async def test_refusals_in_one_minute_are_one_event_and_the_next_refusal_carries_their_count(self) -> None:
        for _ in range(5):
            self.refuse()
        self.assertEqual(self.refused(), [{"reason": "wrong takeover token"}])  # the first, as it always was

        self.now = 1059.5
        self.refuse()  # still inside the minute: answered, and only counted
        self.assertEqual(len(self.refused()), 1)

        self.now = 1060.0
        self.refuse(token=None)
        self.assertEqual(self.refused()[1:], [{"reason": "missing takeover token", "count": 5}])

    async def test_the_count_restarts_with_each_window_and_a_quiet_one_has_none(self) -> None:
        self.refuse()
        self.refuse()
        self.now += 60
        self.refuse()  # carries 1, and opens a window
        self.refuse()
        self.refuse()
        self.now += 60
        self.refuse()  # carries 2
        self.now += 60
        self.refuse()  # nothing was held back in the window before
        wrong = {"reason": "wrong takeover token"}
        self.assertEqual(self.refused(), [wrong, {**wrong, "count": 1}, {**wrong, "count": 2}, wrong])

    async def test_the_right_token_is_audited_every_time_and_leaves_the_window_standing(self) -> None:
        self.refuse()
        self.refuse()
        for _ in range(3):
            self.assertEqual(self.link(self.session_id).status_code, 200)
        viewed = [c for c in self.audit.await_args_list if c.kwargs.get("event_type") == "takeover_link_viewed"]
        self.assertEqual(len(viewed), 3)
        self.assertEqual(len(self.refused()), 2)  # the first read wrote the refusal that had only been counted

        self.refuse()  # still inside the minute: only counted
        self.assertEqual(len(self.refused()), 2)
        self.now += 60
        self.refuse()
        self.assertEqual(self.refused()[-1]["count"], 1)

    def events(self) -> list[tuple[str, str | None, dict]]:
        """Every takeover-link audit event so far, as (event type, session id, details)."""
        return [
            (c.kwargs["event_type"], c.kwargs.get("session_id"), c.kwargs["details"])
            for c in self.audit.await_args_list
            if c.kwargs.get("event_type", "").startswith("takeover_link_")
        ]

    def refusals_reported(self) -> int:
        """What an alert adds up: each event is one refusal, and its count the others it reports."""
        return sum(1 + details.get("count", 0) for details in self.refused())

    async def sweep(self) -> None:
        """What the maintenance sweep runs: the windows that are over."""
        await write_held_takeover_refusals(self.manager, self.refusal_windows, finished_only=True)

    async def shut_down(self) -> None:
        """What a clean shutdown runs: every window."""
        await write_held_takeover_refusals(self.manager, self.refusal_windows)

    async def test_a_burst_then_silence_is_reported_in_full_once_its_minute_is_over(self) -> None:
        for _ in range(5):
            self.refuse()
        self.assertEqual(self.refusals_reported(), 1)  # four are only counted so far

        self.now += 59  # the minute still runs
        await self.sweep()
        self.assertEqual(self.refusals_reported(), 1)
        self.now += 1
        await self.sweep()

        wrong = {"reason": "wrong takeover token"}
        self.assertEqual(self.refused(), [wrong, {**wrong, "count": 3}])
        self.assertEqual(self.refusals_reported(), 5)
        self.now += 60
        await self.sweep()  # written once
        self.assertEqual(self.refusals_reported(), 5)

        self.refuse()  # a refusal after that carries nothing: what was held is written
        self.assertEqual(self.refused()[2:], [wrong])

    async def test_a_burst_that_ends_with_the_right_token_reports_its_tail_before_the_read(self) -> None:
        for _ in range(4):
            self.refuse(token=None)

        self.assertEqual(self.link(self.session_id).status_code, 200)

        missing = {"reason": "missing takeover token"}
        self.assertEqual(
            self.events(),
            [
                ("takeover_link_refused", self.session_id, missing),
                ("takeover_link_refused", self.session_id, {**missing, "count": 2}),
                ("takeover_link_viewed", self.session_id, {}),
            ],
        )
        self.assertEqual(self.refusals_reported(), 4)

    async def test_a_right_token_for_a_session_that_is_not_there_reports_the_tail_too(self) -> None:
        self.refuse()
        self.refuse()

        response = self.client.get("/sessions/no-such-session/takeover-link", headers={"X-Takeover-Token": TAKEOVER})

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.refusals_reported(), 2)

    async def test_a_read_writes_only_its_own_sources_tail(self) -> None:
        other = self.from_host("203.0.113.7")
        self.refuse(other)
        self.refuse(other)

        self.assertEqual(self.link(self.session_id).status_code, 200)  # from the test client's own host

        self.assertEqual(self.refusals_reported(), 1)

    async def test_the_event_of_counted_refusals_names_the_last_and_says_others_named_another_session(self) -> None:
        for session_id in ("walked-1", "walked-2", "walked-3", "walked-4"):
            response = self.client.get(f"/sessions/{session_id}/takeover-link", headers={"X-Takeover-Token": "x"})
            self.assertEqual(response.status_code, 403, response.text)

        await self.shut_down()

        wrong = {"reason": "wrong takeover token"}
        self.assertEqual(
            self.events(),
            [
                ("takeover_link_refused", "walked-1", wrong),
                ("takeover_link_refused", "walked-4", {**wrong, "count": 2, "counted_other_sessions": True}),
            ],
        )

    async def test_a_clean_shutdown_writes_what_a_running_window_holds(self) -> None:
        self.refuse()
        self.refuse()
        self.refuse()

        await self.shut_down()  # every window, over or not

        self.assertEqual(self.refusals_reported(), 3)

    async def test_what_was_counted_is_written_as_the_operator_who_was_refused(self) -> None:
        alice = OperatorIdentity(id="alice", name="Alice", source="token")
        written_as: list[OperatorIdentity] = []
        self.audit.side_effect = lambda **_kwargs: written_as.append(get_current_operator())
        self.refusal_windows.record("operator:alice", RefusedCall("s1", "wrong takeover token", alice), now=self.now)
        self.refusal_windows.record("operator:alice", RefusedCall("s1", "wrong takeover token", alice), now=self.now)

        await self.shut_down()  # from the shutdown or the sweep: no request, so nobody is acting

        self.assertEqual(written_as, [alice])
        self.assertEqual(get_current_operator().id, "anonymous")

    async def test_a_write_that_fails_is_logged_and_what_it_was_to_report_is_written_at_the_next_try(self) -> None:
        for _ in range(3):
            self.refuse()
        self.audit.side_effect = RuntimeError("audit store down")

        with self.assertLogs("app.routes.sessions", level="WARNING") as logged:
            self.now += 60
            await self.sweep()  # raises nothing: no request waits on it

        self.assertEqual(len(logged.records), 1)
        self.assertIn("2 refused takeover-link calls", logged.records[0].getMessage())
        self.audit.side_effect = None
        self.audit.reset_mock()
        await self.sweep()

        self.assertEqual(self.refused(), [{"reason": "wrong takeover token", "count": 1}])

    async def test_a_tail_that_cannot_be_written_fails_the_read_as_its_own_event_would(self) -> None:
        self.refuse()
        self.refuse()
        self.audit.side_effect = RuntimeError("audit store down")
        client = TestClient(self.app, raise_server_exceptions=False)

        response = client.get(f"/sessions/{self.session_id}/takeover-link", headers={"X-Takeover-Token": TAKEOVER})

        self.assertEqual(response.status_code, 500, response.text)
        self.audit.side_effect = None
        self.audit.reset_mock()
        self.assertEqual(self.link(self.session_id).status_code, 200)
        self.assertEqual(self.refused(), [{"reason": "wrong takeover token"}])  # the tail, at the next read

    async def test_each_client_host_has_its_own_window(self) -> None:
        first, second = self.from_host("203.0.113.1"), self.from_host("203.0.113.2")
        for client in (first, second, first, second):
            self.refuse(client)
        self.assertEqual(len(self.refused()), 2)

    async def test_a_claimed_operator_header_is_not_a_source(self) -> None:
        # Tokenless, X-Operator-Id is a label anyone can set: a new label must not buy a new event.
        from app.middleware.http import install_controller_http_middleware

        settings = self.manager.settings
        settings.api_bind_scope = "loopback"
        settings.api_allow_tokenless_loopback = True
        settings.api_bearer_token = None
        app = FastAPI()
        app.include_router(create_sessions_router(manager=self.manager))
        install_controller_http_middleware(app, settings=settings, rate_limiter=None, metrics=Mock(enabled=False))
        client = TestClient(app)
        for operator in ("alice", "bob", "carol"):
            headers = {"X-Takeover-Token": "wrong", "X-Operator-Id": operator}
            response = client.get(f"/sessions/{self.session_id}/takeover-link", headers=headers)
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(len(self.refused()), 1)

    @patch("app.routes.sessions.TAKEOVER_REFUSAL_SOURCES_MAX", 2)
    async def test_the_map_is_bounded_and_the_oldest_source_is_forgotten(self) -> None:
        a, b, c = (self.from_host(f"203.0.113.{n}") for n in (1, 2, 3))
        for client in (a, b, c):
            self.refuse(client)
        self.refuse(b)  # still remembered, so only counted
        self.assertEqual(len(self.refused()), 3)

        self.refuse(a)  # pushed out by c, so it audits as a new source
        self.assertEqual(self.refused()[3:], [{"reason": "wrong takeover token"}])

    async def test_a_failed_append_opens_no_window_and_the_next_refusal_writes_its_event_with_the_count(self) -> None:
        self.audit.side_effect = RuntimeError("audit store down")
        client = TestClient(self.app, raise_server_exceptions=False)

        response = client.get(f"/sessions/{self.session_id}/takeover-link", headers={"X-Takeover-Token": "wrong"})

        self.assertEqual(response.status_code, 500, response.text)  # an append failure answers as it always did
        self.assertEqual(self.refused(), [{"reason": "wrong takeover token"}])

        self.audit.side_effect = None
        self.refuse()  # the audit store is back: this writes, carrying the refusal whose event was lost

        self.assertEqual(self.refused()[1:], [{"reason": "wrong takeover token", "count": 1}])

        self.refuse()  # that event opened a window: this one is only counted, as before
        self.assertEqual(len(self.refused()), 2)

    async def test_a_refusal_during_a_failing_append_is_counted(self) -> None:
        started, fail = asyncio.Event(), asyncio.Event()

        async def append_that_fails_later(**_kwargs) -> None:
            started.set()
            await fail.wait()
            raise RuntimeError("audit store down")

        self.audit.side_effect = append_that_fails_later
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://t")
        self.addAsyncCleanup(client.aclose)
        url = f"/sessions/{self.session_id}/takeover-link"
        headers = {"X-Takeover-Token": "wrong"}

        first = asyncio.ensure_future(client.get(url, headers=headers))
        await asyncio.wait_for(started.wait(), timeout=5)
        during = await client.get(url, headers=headers)  # no append of its own: the window stands for now
        self.assertEqual(during.status_code, 403, during.text)
        fail.set()
        with self.assertRaises(RuntimeError):
            await first

        self.audit.side_effect = None
        after = await client.get(url, headers=headers)

        self.assertEqual(after.status_code, 403, after.text)
        self.assertEqual(self.refused()[-1], {"reason": "wrong takeover token", "count": 2})

    async def test_a_cancelled_append_counts_as_not_written(self) -> None:
        async def append_cancelled_meanwhile(**_kwargs) -> None:
            raise asyncio.CancelledError

        self.audit.side_effect = append_cancelled_meanwhile
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://t")
        self.addAsyncCleanup(client.aclose)
        url = f"/sessions/{self.session_id}/takeover-link"
        headers = {"X-Takeover-Token": "wrong"}

        with self.assertRaises(asyncio.CancelledError):
            await client.get(url, headers=headers)

        self.audit.side_effect = None
        response = await client.get(url, headers=headers)

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.refused()[-1], {"reason": "wrong takeover token", "count": 1})

    @patch("app.routes.sessions.TAKEOVER_REFUSAL_SOURCES_MAX", 2)
    async def test_a_source_opening_a_new_window_is_the_newest_again(self) -> None:
        a, b, c = (self.from_host(f"203.0.113.{n}") for n in (1, 2, 3))
        self.refuse(a)
        self.now += 30
        self.refuse(b)
        self.now += 30  # a's minute is over, b's is not
        self.refuse(a)  # a new window: a is now newer than b
        self.refuse(c)  # so it is b that is pushed out
        self.assertEqual(len(self.refused()), 4)

        self.refuse(a)  # still remembered
        self.assertEqual(len(self.refused()), 4)
        self.refuse(b)  # forgotten, so it audits as new
        self.assertEqual(len(self.refused()), 5)


class TheControllerWritesWhatItsRouteHoldsTests(unittest.TestCase):
    """app.main gives the route, the maintenance sweep and the shutdown the same windows."""

    SOURCE = "host:203.0.113.9"

    def setUp(self) -> None:
        self.windows = main_module.takeover_refusals
        self.windows._windows.clear()
        self.addCleanup(self.windows._windows.clear)
        self.audit = AsyncMock()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(main_module.manager.audit, "append", new=self.audit))
        stack.enter_context(patch.object(main_module.manager.settings, "takeover_token", TAKEOVER))
        stack.enter_context(
            patch.object(main_module, "validate_runtime_policy", return_value=Mock(errors=[], warnings=[]))
        )
        for service in (main_module.manager, main_module.job_queue, main_module.cron_service, main_module.maintenance):
            for name in ("startup", "shutdown"):
                stack.enter_context(patch.object(service, name, new=AsyncMock()))

    def hold_two(self, *, started: float) -> None:
        """A first refusal, whose event is taken as written, and two that are only counted."""
        for _ in range(3):
            self.windows.record(self.SOURCE, refused_call("s1"), now=started)

    def refused(self) -> list[dict]:
        calls = self.audit.await_args_list
        return [c.kwargs["details"] for c in calls if c.kwargs.get("event_type") == "takeover_link_refused"]

    def test_a_refusal_through_the_app_is_counted_in_the_windows_the_sweep_and_the_shutdown_write(self) -> None:
        client = TestClient(main_module.app, base_url="http://127.0.0.1", client=("203.0.113.9", 50000))

        for _ in range(3):
            response = client.get("/sessions/s1/takeover-link", headers={"X-Takeover-Token": "wrong"})
            self.assertEqual(response.status_code, 403, response.text)

        ((source, call, held),) = self.windows.take_held()
        self.assertEqual((source, call.session_id, held), (self.SOURCE, "s1", HeldBack(1, False)))

    def test_the_sweep_writes_a_window_that_is_over_and_leaves_one_that_runs(self) -> None:
        self.hold_two(started=time.monotonic())

        asyncio.run(main_module.maintenance.write_held_refusals())
        self.assertEqual(self.refused(), [])

        self.windows._windows.clear()
        self.hold_two(started=time.monotonic() - 61)
        asyncio.run(main_module.maintenance.write_held_refusals())

        self.assertEqual(self.refused(), [{"reason": "wrong takeover token", "count": 1}])

    def test_a_clean_shutdown_writes_a_window_that_still_runs_before_the_manager_is_shut_down(self) -> None:
        order: list[str] = []
        self.audit.side_effect = lambda **_kwargs: order.append("refusals written")
        main_module.manager.shutdown.side_effect = lambda: order.append("manager shut down")
        with TestClient(main_module.app, base_url="http://127.0.0.1"):
            self.hold_two(started=time.monotonic())
            self.assertEqual(self.refused(), [])

        self.assertEqual(self.refused(), [{"reason": "wrong takeover token", "count": 1}])
        self.assertEqual(order, ["refusals written", "manager shut down"])


class AgentPackagesTests(unittest.TestCase):
    @unittest.skipUnless((REPO / "client").is_dir(), "the agent-facing packages are not shipped in the image")
    def test_no_agent_facing_package_calls_the_endpoint(self) -> None:
        for package in ("client", "packaging/auto-browser-mcp", "integrations/langchain"):
            for path in (REPO / package).rglob("*.py"):
                self.assertNotIn("takeover-link", path.read_text(encoding="utf-8"), str(path))


API_SENTINEL = "api-sentinel-0123456789abcdefghijklmnopqrs"
_RECORDER = """#!/usr/bin/env bash
# Records this command's argv, one argument per line, then runs the real one (or answers for curl).
for arg in "$0" "$@"; do printf '%s\\n' "$arg"; done >> "${STUB_LOG}/argv"
if [[ "$(basename "$0")" == curl ]]; then
  cat > "${STUB_LOG}/curl-stdin"
  if [[ -n "${STUB_CURL_STATUS:-}" ]]; then
    echo "curl: (22) The requested URL returned error: ${STUB_CURL_STATUS}" >&2
    exit 22
  fi
  printf '{"session_id": "abc123", "takeover_url": "http://127.0.0.1:16080/vnc.html#password=%s"}' "${STUB_PASSWORD}"
  exit 0
fi
exec "${REAL_PYTHON3}" "$@"
"""


def _opener() -> str:
    return _DASHBOARD_HTML.split("async function openTakeover(")[1].split("\n}\n")[0]


class DashboardAndScriptTests(unittest.TestCase):
    def test_the_dashboard_opens_the_takeover_link_endpoint(self) -> None:
        self.assertIn("/takeover-link", _opener())

    def test_the_dashboard_sends_the_takeover_token_it_asked_for_once_per_tab(self) -> None:
        opener = _opener()
        self.assertIn("X-Takeover-Token", opener)
        self.assertIn("takeoverToken(", opener)
        asker = _DASHBOARD_HTML.split("function takeoverToken(")[1].split("\n}\n")[0]
        self.assertIn("sessionStorage.getItem(TAKEOVER_TOKEN_KEY)", asker)
        self.assertIn("sessionStorage.setItem(TAKEOVER_TOKEN_KEY", asker)
        self.assertIn("TAKEOVER_TOKEN", asker)
        self.assertIn("data/takeover-token", asker)
        self.assertIn("const TAKEOVER_TOKEN_KEY = 'ab_takeover_token';", _DASHBOARD_HTML)
        # A 403 clears the stored token; the next click asks again (W2-18: never a prompt behind the popup).
        self.assertIn("err.status === 403", opener)
        self.assertIn("sessionStorage.removeItem(TAKEOVER_TOKEN_KEY)", opener)

    def test_the_login_wizard_opens_its_takeover_window_through_open_takeover(self) -> None:
        wizard = _DASHBOARD_HTML.split("const takeover = document.getElementById('wizard-takeover');")[1]
        wizard = wizard.split("\n}\n")[0]
        self.assertIn("Open takeover window to log in", wizard)
        self.assertIn("openTakeover(event, ", wizard)

    def test_the_script_is_here_when_scripts_is(self) -> None:
        if not (REPO / "scripts").is_dir():
            self.skipTest("scripts/ is not shipped in the image")
        self.assertTrue(SCRIPT.is_file(), SCRIPT)

    @unittest.skipUnless(SCRIPT.is_file() and BASH, "scripts/ is not here, or the test " + NO_BASH_REASON)
    def test_the_script_parses(self) -> None:
        subprocess.run([BASH, "-n", str(SCRIPT)], check=True)

    def _tree(self, *, takeover_in_env: bool = True) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "scripts").mkdir()
        for name in ("takeover_link.sh", "load_env.sh"):
            shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
        takeover = f"TAKEOVER_TOKEN={TAKEOVER}\n" if takeover_in_env else ""
        (root / ".env").write_text(f"API_PORT=18123\nAPI_BEARER_TOKEN={API_SENTINEL}\n{takeover}", encoding="utf-8")
        (root / "bin").mkdir()
        (root / "log").mkdir()
        for name in ("curl", "python3"):
            stub = root / "bin" / name
            stub.write_text(_RECORDER, encoding="utf-8")
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        return root

    def _run(self, root: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
        env = {
            **extra,
            "PATH": f"{root / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(root),
            "STUB_LOG": str(root / "log"),
            "STUB_PASSWORD": SENTINEL,
            "REAL_PYTHON3": shutil.which("python3") or "/usr/bin/python3",
        }
        return subprocess.run(
            [BASH, str(root / "scripts" / "takeover_link.sh"), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    @unittest.skipUnless(SCRIPT.is_file() and BASH and os.name != "nt", POSIX_SCRIPT_REASON)
    def test_the_script_reads_env_sends_both_tokens_on_stdin_and_prints_the_link(self) -> None:
        root = self._tree()
        result = self._run(root, "abc123")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"http://127.0.0.1:16080/vnc.html#password={SENTINEL}\n")
        argv = (root / "log" / "argv").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:18123/sessions/abc123/takeover-link", argv.splitlines())
        # Both stubs record every argv they were started with: no token, no password, no response there.
        for secret in (API_SENTINEL, TAKEOVER, SENTINEL):
            self.assertNotIn(secret, argv)
            self.assertNotIn(secret, result.stderr)
        config = (root / "log" / "curl-stdin").read_text(encoding="utf-8").splitlines()
        self.assertIn(f'header = "Authorization: Bearer {API_SENTINEL}"', config)
        self.assertIn(f'header = "X-Takeover-Token: {TAKEOVER}"', config)

    @unittest.skipUnless(SCRIPT.is_file() and BASH and os.name != "nt", POSIX_SCRIPT_REASON)
    def test_w2_25_without_takeover_token_the_script_reads_data_takeover_token(self) -> None:
        root = self._tree(takeover_in_env=False)
        (root / "data").mkdir()
        (root / "data" / "takeover-token").write_text(TAKEOVER + "\n", encoding="utf-8")
        result = self._run(root, "abc123")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"http://127.0.0.1:16080/vnc.html#password={SENTINEL}\n")
        self.assertNotIn("No takeover token", result.stderr)
        self.assertNotIn(TAKEOVER, result.stderr)
        self.assertNotIn(TAKEOVER, (root / "log" / "argv").read_text(encoding="utf-8"))
        config = (root / "log" / "curl-stdin").read_text(encoding="utf-8").splitlines()
        self.assertIn(f'header = "X-Takeover-Token: {TAKEOVER}"', config)

    @unittest.skipUnless(SCRIPT.is_file() and BASH and os.name != "nt", POSIX_SCRIPT_REASON)
    def test_w2_24_without_a_takeover_token_the_script_warns_and_names_where_it_looks(self) -> None:
        root = self._tree(takeover_in_env=False)
        result = self._run(root, "abc123")

        self.assertIn("No takeover token", result.stderr)
        self.assertIn("TAKEOVER_TOKEN", result.stderr)
        self.assertIn("data/takeover-token", result.stderr)
        self.assertNotIn(API_SENTINEL, result.stderr)

    @unittest.skipUnless(SCRIPT.is_file() and BASH and os.name != "nt", POSIX_SCRIPT_REASON)
    def test_w2_24_a_refused_request_says_what_a_403_means(self) -> None:
        root = self._tree()
        result = self._run(root, "abc123", STUB_CURL_STATUS="403")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("A 403 means the takeover token was missing or wrong", result.stderr)
        for secret in (API_SENTINEL, TAKEOVER):
            self.assertNotIn(secret, result.stderr)

    @unittest.skipUnless(SCRIPT.is_file() and BASH and os.name != "nt", POSIX_SCRIPT_REASON)
    def test_the_script_refuses_an_id_that_does_not_start_with_a_letter_or_digit(self) -> None:
        root = self._tree()
        for bad in (".", "..", "-x", "_a", "a/b", "a b"):
            with self.subTest(session_id=bad):
                result = self._run(root, bad)
                self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((root / "log" / "argv").exists() and "curl" in (root / "log" / "argv").read_text())
        self.assertEqual(self._run(root, "a.b_c-1").returncode, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
