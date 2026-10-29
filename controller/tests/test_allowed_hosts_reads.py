"""Every read the agent receives is refused if its page moved while it was read.

Checking the page's URL before a read and again after it compares two instants. A page
that went to a refused host and came back, one the page guard sent to about:blank, and an
active page that closed and handed the session to another tab all passed both checks, and
what was read was the refused page's (R-02, R-03). Each read now captures the active page
and its count of refused commits before it reads, and refuses afterwards if either changed
or the page is refused now. These use fakes, so the page moves during the read every run.
"""

from __future__ import annotations

import asyncio
import json
import traceback
import weakref
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from playwright.async_api import Error as PlaywrightError

from app.action_errors import BrowserActionError
from app.browser import page_read
from app.browser.services import sessions as sessions_module
from app.browser_manager import BrowserSession
from app.mcp_transport import McpHttpTransport
from app.models import McpToolCallRequest
from app.network_inspector import url_origin
from app.routes._utils import OFF_LIST_PAGE_DETAIL
from app.routes.extensions.cdp import cdp_router
from app.tool_gateway import McpToolGateway
from app.tool_inputs import (
    DragDropInput,
    EvalJsInput,
    FindElementsInput,
    GetPageHtmlInput,
    GetStorageInput,
    SetStorageInput,
    VisionFindInput,
    WaitForSelectorInput,
)
from app.utils import UTC
from tests.test_allowed_hosts_rechecks import ALLOWED, REFUSED, RecordingLock, manager_for

LEAKED = "REFUSED-CONTENT"
QUICK_TIMEOUT = 0.2
HANG_SECONDS = 3.0


# ── How a page moves while it is read ────────────────────────────────────────


def returns(manager, session, page) -> None:
    """Allowed, refused, allowed again: both URL checks see an allowed page."""
    manager.page_provenance.committed(page, REFUSED, url_refused=True)
    manager.page_provenance.committed(page, ALLOWED, url_refused=False)


def blank(manager, session, page) -> None:
    """Refused, then the page guard's blank: about:blank passes a URL check."""
    manager.page_provenance.committed(page, REFUSED, url_refused=True)
    manager.page_provenance.blank_started(page)
    manager.page_provenance.committed(page, "about:blank", url_refused=False)
    page.url = "about:blank"


def swap(manager, session, page) -> None:
    """The page went refused, was blanked and closed, and the session moved to another tab."""
    manager.page_provenance.committed(page, REFUSED, url_refused=True)
    page.url = "about:blank"
    page.closed = True
    manager.page_provenance.forget(page)
    session.page = OtherPage()


def lands(manager, session, page) -> None:
    """On a refused URL whose commit the page guard has not seen yet."""
    page.url = REFUSED


def closes(manager, session, page) -> None:
    """Refused, blanked, then closed: the only tab, so the session keeps it, and its count is forgotten."""
    blank(manager, session, page)
    page.closed = True
    manager.page_provenance.forget(page)


MOVES = {"returns": returns, "blank": blank, "swap": swap, "lands": lands, "closes": closes}


class OtherPage:
    """The tab the session moved to: allowed, and never moves."""

    url = ALLOWED

    def __init__(self) -> None:
        self.mouse = SimpleNamespace(move=AsyncMock(), down=AsyncMock(), up=AsyncMock())

    async def _moved(self, *_args, **_kwargs) -> None:
        pass

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        return "other tab"

    async def content(self) -> str:
        return "<p>other tab</p>"

    async def evaluate(self, *_args) -> str:
        return "other tab"


class ReadPage:
    """An allowed page that moves the first time it is read, and answers every read with LEAKED."""

    def __init__(self, evaluated: object = LEAKED) -> None:
        self.url = ALLOWED
        self.closed = False
        self.move = None  # set by read_session
        self._evaluated = evaluated
        self.mouse = SimpleNamespace(move=AsyncMock(side_effect=self._moved), down=AsyncMock(), up=AsyncMock())

    async def _moved(self, *_args, **_kwargs) -> None:
        if self.move is not None:
            move, self.move = self.move, None
            move()

    async def title(self) -> str:
        await self._moved()
        return LEAKED

    async def content(self) -> str:
        await self._moved()
        return f"<p>{LEAKED}</p>"

    async def evaluate(self, *_args) -> object:
        await self._moved()
        return self._evaluated

    async def wait_for_selector(self, *_args, **_kwargs) -> None:
        await self._moved()

    def is_closed(self) -> bool:
        return self.closed


def read_session(manager, kind: str, tmp_path: Path, *, evaluated: object = LEAKED) -> SimpleNamespace:
    page = ReadPage(evaluated)
    session = SimpleNamespace(
        id="s1",
        page=page,
        lock=asyncio.Lock(),
        attached_pages=weakref.WeakSet(),
        context=SimpleNamespace(pages=[page]),
        console_messages=[],
        page_errors=[],
        request_failures=[],
        downloads=[],
        artifact_dir=tmp_path,
        last_action=None,
    )
    page.move = lambda: MOVES[kind](manager, session, page)
    manager.get_session = AsyncMock(return_value=session)
    manager.sessions["s1"] = session
    return session


def writes_a_screenshot(manager, session, tmp_path: Path, *, moves: bool = True) -> Path:
    """Captures write their file while the page is read (and moves, unless `moves` is False)."""
    shot = tmp_path / "shot.png"

    async def capture(_session, label: str, **_kwargs) -> dict:
        shot.write_bytes(LEAKED.encode())
        if moves:
            await session.page._moved()
        return {"path": str(shot), "url": f"/artifacts/s1/{label}.png"}

    manager.artifacts.capture_screenshot = AsyncMock(side_effect=capture)
    manager.pii_scrubber.screenshot_enabled = False
    return shot


KINDS = pytest.mark.parametrize("kind", sorted(MOVES))


def gateway_for(manager) -> McpToolGateway:
    return McpToolGateway(manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())


# ── Observe, screenshots, action snapshots, share frames ────────────────────


def stub_observation(manager, session) -> None:
    async def evaluate(page, *_args):
        await page._moved()
        return []

    async def summary(page, text_limit: int = 2000) -> dict:
        await page._moved()
        return {
            "title": LEAKED,
            "active_element": None,
            "text_excerpt": LEAKED,
            "dom_outline": {},
            "accessibility_outline": {},
        }

    manager.element_ids.evaluate = AsyncMock(side_effect=evaluate)
    manager.observation.page_summary = AsyncMock(side_effect=summary)
    manager.observation._extract_ocr_if_needed = AsyncMock(return_value=None)
    manager.observation.accessibility_outline = AsyncMock(return_value={"available": False, "nodes": []})
    manager._session_summary = AsyncMock(return_value={"id": "s1"})
    manager.tabs.summaries = AsyncMock(return_value=[])
    manager._current_takeover_url = lambda _session: None
    manager.remote_access = SimpleNamespace(session_info=lambda _session: {})


@KINDS
@pytest.mark.parametrize("preset", ["text", "fast", "normal", "rich"])
async def test_an_observation_is_refused_when_its_page_moved_while_it_was_read(
    tmp_path: Path, kind: str, preset: str
) -> None:
    manager = manager_for(tmp_path)
    session = read_session(manager, kind, tmp_path)
    stub_observation(manager, session)
    shot = writes_a_screenshot(manager, session, tmp_path)

    with pytest.raises(PermissionError, match="ALLOWED_HOSTS"):
        await manager.observation.observation_payload(session, preset=preset)

    assert not shot.exists()


@KINDS
async def test_a_screenshot_is_refused_and_deleted_when_its_page_moved_while_it_was_taken(
    tmp_path: Path, kind: str
) -> None:
    manager = manager_for(tmp_path)
    session = read_session(manager, kind, tmp_path)
    shot = writes_a_screenshot(manager, session, tmp_path)

    with pytest.raises(PermissionError, match="ALLOWED_HOSTS"):
        await manager.observation.capture_session_screenshot(session, "shot")

    assert not shot.exists()


@KINDS
async def test_an_action_snapshot_is_refused_when_its_page_moved_while_it_was_read(tmp_path: Path, kind: str) -> None:
    manager = manager_for(tmp_path)
    manager.settings.pii_scrub_action_screenshots = False
    session = read_session(manager, kind, tmp_path)
    stub_observation(manager, session)
    shot = writes_a_screenshot(manager, session, tmp_path, moves=False)

    with pytest.raises(PermissionError, match="ALLOWED_HOSTS"):
        await manager.observation.light_snapshot(session, label="action")

    assert not shot.exists()


@KINDS
async def test_a_share_frame_is_refused_when_its_page_moved_while_its_title_was_read(tmp_path: Path, kind: str) -> None:
    """R-04: the title was read after the frame's last check and returned unchecked."""
    manager = manager_for(tmp_path)
    session = read_session(manager, kind, tmp_path)
    shot = writes_a_screenshot(manager, session, tmp_path, moves=False)

    with pytest.raises(PermissionError, match="ALLOWED_HOSTS"):
        await manager.observation.share_snapshot("s1")

    assert not shot.exists()


# ── Titles in listings and records ───────────────────────────────────────────


@KINDS
async def test_a_tab_whose_page_moved_while_its_title_was_read_has_no_title(tmp_path: Path, kind: str) -> None:
    manager = manager_for(tmp_path)
    session = read_session(manager, kind, tmp_path)
    session.context.pages = [session.page]

    tabs = await manager.tabs.summaries(session)

    assert LEAKED not in str(tabs), tabs


@KINDS
async def test_a_session_summary_has_no_title_when_its_page_moved_while_it_was_read(tmp_path: Path, kind: str) -> None:
    manager = manager_for(tmp_path)
    session = read_session(manager, kind, tmp_path)

    _, title, _ = await manager.session_lifecycle._page_snapshot(session)

    assert title == ""


@KINDS
async def test_an_approval_request_has_no_title_when_its_page_moved_while_it_was_read(
    tmp_path: Path, kind: str
) -> None:
    manager = manager_for(tmp_path)
    session = read_session(manager, kind, tmp_path)
    manager._current_takeover_url = lambda _session: None
    manager.remote_access = SimpleNamespace(session_info=lambda _session: {})
    manager.session_lifecycle.isolation_payload = lambda _session: {}
    manager.auth_profiles.session_auth_state_info = lambda _session: {}

    observation = await manager.actions._approval_observation(session)

    assert observation["title"] == "", observation


async def write_empty_state(_context, destination: Path) -> dict:
    """What write_storage_state does: writes the state where it is told, and says where."""
    destination.write_text("{}", encoding="utf-8")
    return {"path": str(destination), "encrypted": False}


@KINDS
async def test_a_saved_auth_profile_has_no_title_when_its_page_moved_while_it_was_read(
    tmp_path: Path, kind: str
) -> None:
    manager = manager_for(tmp_path)
    session = read_session(manager, kind, tmp_path)
    session.last_auth_state_path, session.auth_profile_name = None, None
    manager.auth_state.write_storage_state = AsyncMock(side_effect=write_empty_state)
    manager.auth_profiles.current_platform = lambda _session: "test"

    saved = await manager.auth_profiles.save_for_session(session, "p1")

    assert saved["metadata"]["saved_from_title"] == "", saved["metadata"]


# ── What an action finds after it ran ────────────────────────────────────────


async def test_a_bot_challenge_is_not_probed_on_a_page_refused_before_the_probe(tmp_path: Path) -> None:
    """Read F9: the probe read a refused page's title and iframes, and a taint that cleared before it
    returned left nothing for the after-check to catch. A refused page is not probed at all."""
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    page = session.page
    manager.page_provenance.committed(page, REFUSED, url_refused=True)
    page.url = "about:blank"

    async def probe(*_args) -> dict:
        page.url = ALLOWED
        manager.page_provenance.committed(page, ALLOWED, url_refused=False)
        return {"title": f"access denied {LEAKED}", "text": "", "iframes": []}

    page.evaluate = AsyncMock(side_effect=probe)

    assert await manager._check_bot_challenge(session) is None
    page.evaluate.assert_not_awaited()


@KINDS
async def test_a_bot_challenge_on_a_page_that_moved_while_it_was_probed_is_refused_not_described(
    tmp_path: Path, kind: str
) -> None:
    """R-05: the probe's title and iframe sources went to the agent in the error details."""
    manager = manager_for(tmp_path)
    probe = {"title": f"access denied - {LEAKED}", "text": "", "iframes": [f"http://10.0.0.7/{LEAKED}"]}
    session = read_session(manager, kind, tmp_path, evaluated=probe)

    with pytest.raises(PermissionError, match="ALLOWED_HOSTS"):
        await manager._check_bot_challenge(session)


async def test_control_a_bot_challenge_on_a_page_that_stayed_put_is_described(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    probe = {"title": "access denied", "text": "", "iframes": []}
    session = read_session(manager, "returns", tmp_path, evaluated=probe)
    session.page.move = None

    challenge = await manager._check_bot_challenge(session)

    assert challenge is not None and challenge["signal"] == "access denied"


# ── MCP tools and the DOM resource ───────────────────────────────────────────


MCP_READS = {
    "eval_js": ("_eval_js", EvalJsInput(session_id="s1", expression="1")),
    # Its shortest timeout: a wait refused because its page moved answers no sooner than that (CR-3).
    "wait_for_selector": ("_wait_for_selector", WaitForSelectorInput(session_id="s1", selector="p", timeout_ms=100)),
    "get_html": ("_get_html", GetPageHtmlInput(session_id="s1")),
    "get_text": ("_get_html", GetPageHtmlInput(session_id="s1", text_only=True)),
    "find_by_query": ("_find_elements", FindElementsInput(session_id="s1", query="x")),
    "find_by_selector": ("_find_elements", FindElementsInput(session_id="s1", selector="p")),
    "get_storage": ("_get_local_storage", GetStorageInput(session_id="s1")),
    "set_storage": ("_set_local_storage", SetStorageInput(session_id="s1", key="k", value="v")),
    "drag_drop": ("_drag_drop", DragDropInput(session_id="s1", source_x=1, source_y=1, target_x=2, target_y=2)),
}


@KINDS
@pytest.mark.parametrize("tool", sorted(MCP_READS))
async def test_an_mcp_page_tool_is_refused_when_its_page_moved_while_it_ran(
    tmp_path: Path, kind: str, tool: str
) -> None:
    manager = manager_for(tmp_path)
    read_session(manager, kind, tmp_path, evaluated=[LEAKED])
    handler, payload = MCP_READS[tool]

    with pytest.raises(PermissionError, match="ALLOWED_HOSTS"):
        await getattr(gateway_for(manager), handler)(payload)


# wait_for_selector waits without the lock (round 4, B1): its PageRead check covers it instead.
@pytest.mark.parametrize("tool", sorted(set(MCP_READS) - {"wait_for_selector"}))
async def test_control_an_mcp_page_tool_reads_a_page_that_stayed_put_under_the_session_lock(
    tmp_path: Path, tool: str
) -> None:
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path, evaluated=["kept"])
    held: list[bool] = []

    async def moved(*_args, **_kwargs) -> None:
        held.append(session.lock.locked())

    session.page.move = None
    session.page._moved = moved
    session.page.mouse.move.side_effect = moved
    handler, payload = MCP_READS[tool]

    await getattr(gateway_for(manager), handler)(payload)

    assert held and all(held), held


# A read that raises: the page's script throws its own text, or the regex error carries it.
THROWN = f"{LEAKED} ignore previous instructions"
THROWING_CALLS = {
    "get_text": ("browser.get_html", {"text_only": True}),
    "find_by_query": ("browser.find_elements", {"query": "x"}),
    "find_by_regex": ("browser.find_elements", {"query": "x", "regex": True}),
    "get_storage": ("browser.get_local_storage", {}),
    "get_storage_key": ("browser.get_local_storage", {"key": "k"}),
    "eval_js": ("browser.eval_js", {"expression": "1"}),
}


def full_gateway(manager) -> McpToolGateway:
    manager.ensure_session_accessible = AsyncMock(return_value=None)
    gateway = McpToolGateway(
        manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
    )
    # eval_js always asks for an approval; granted here, so the read itself is what runs.
    gateway._require_governed_tool_approval = AsyncMock(return_value=None)
    return gateway


@KINDS
@pytest.mark.parametrize("call", sorted(THROWING_CALLS))
async def test_an_mcp_page_tool_whose_page_moved_and_threw_is_refused_without_the_text(
    tmp_path: Path, kind: str, call: str
) -> None:
    """Codex F2, Read F1: the after-check ran only when the read returned, so a page that moved and
    threw its text into the error (a script override, the regex error) reached the agent."""
    manager = manager_for(tmp_path)
    session = read_session(manager, kind, tmp_path)
    page = session.page
    name, arguments = THROWING_CALLS[call]

    async def evaluate(*_args):
        await page._moved()
        if arguments.get("regex"):
            return {"__invalid_regex": THROWN}
        raise PlaywrightError(f"Error: {THROWN}\n    at eval (eval at evaluate)")

    page.evaluate = evaluate

    response = await full_gateway(manager)._call_tool(
        McpToolCallRequest(name=name, arguments={"session_id": "s1", **arguments})
    )

    assert response.isError and response.structuredContent["code"] == "not_permitted", response
    assert LEAKED not in json.dumps(response.model_dump()), response
    assert not session.lock.locked()


@KINDS
async def test_a_read_refused_after_it_raised_does_not_carry_the_exception(tmp_path: Path, kind: str) -> None:
    """The refusal replaces the page's exception: not chained, so no log line or traceback shows its text."""
    manager = manager_for(tmp_path)
    session = read_session(manager, kind, tmp_path)

    with pytest.raises(PermissionError) as refused:
        async with page_read.locked_read(manager, session):
            await session.page._moved()
            raise PlaywrightError(f"Error: {THROWN}")

    assert refused.value.__cause__ is None and refused.value.__suppress_context__
    assert LEAKED not in "".join(traceback.format_exception(refused.value))


# How long a fake page summary waits for the screenshot beside it to be captured and checked (no real I/O).
AFTER_THE_CAPTURE_SECONDS = 0.05


@pytest.mark.parametrize("path", ["observe", "light_snapshot", "share", "screenshot"])
async def test_a_read_whose_page_moved_and_raised_is_refused_on_every_start_read_path(
    tmp_path: Path, path: str
) -> None:
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    page = session.page

    async def throws(*_args, **_kwargs):
        await page._moved()
        raise PlaywrightError(f"Error: {THROWN}")

    async def throws_once_the_capture_is_checked(*_args, **_kwargs):
        # light_snapshot runs its summary alongside the screenshot (shielded since round 4 part 2), whose
        # own check refused a move made before it and hid light_snapshot's (round 4d mutant
        # 4a-a5-light_snapshot_error_unchecked survived).
        await asyncio.sleep(AFTER_THE_CAPTURE_SECONDS)
        await throws()

    # Each path's own read throws, after any screenshot it takes has been checked.
    writes_a_screenshot(manager, session, tmp_path, moves=False)
    if path == "observe":
        manager.element_ids.evaluate = AsyncMock(side_effect=throws)
    elif path == "light_snapshot":
        manager.observation.page_summary = AsyncMock(side_effect=throws_once_the_capture_is_checked)
    elif path == "share":
        page.title = throws
    else:
        manager.artifacts.capture_screenshot = AsyncMock(side_effect=throws)
    reads = {
        "observe": lambda: manager.observation.observation_payload(session, preset="text"),
        "light_snapshot": lambda: manager.observation.light_snapshot(session, label="before"),
        "share": lambda: manager.observation.share_snapshot("s1"),
        "screenshot": lambda: manager.observation.capture_session_screenshot(session, "manual"),
    }

    with pytest.raises(PermissionError) as refused:
        await reads[path]()

    assert LEAKED not in "".join(traceback.format_exception(refused.value))


async def test_a_screenshot_reports_the_url_its_checked_capture_saw(tmp_path: Path) -> None:
    """Codex F4, Read F5: the URL was read after the summary's await, by when the page had moved."""
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    session.page.move = None
    writes_a_screenshot(manager, session, tmp_path, moves=False)

    async def summary(_session) -> dict:
        session.page.url = REFUSED_WITH_DATA
        manager.page_provenance.committed(session.page, REFUSED_WITH_DATA, url_refused=True)
        return {"id": "s1"}

    manager._session_summary = summary
    manager._current_takeover_url = lambda _session: None

    shot = await manager.observation.capture_screenshot("s1")

    assert shot["url"] == ALLOWED, shot


# ── A capture cut off, and the screenshots other tools reuse (Read F7) ───────

STRAY = "20261027T000000000000Z-observe.png"


def png(path: Path) -> None:
    Image.new("RGB", (8, 8), "white").save(path)


async def test_a_capture_cancelled_while_its_page_moved_leaves_no_frame(tmp_path: Path) -> None:
    """Cancelled inside the write (a harness deadline, a job cancel), the check after it never ran."""
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)

    async def cut_off(_session, label: str, **kwargs) -> dict:
        target = kwargs.get("target") or {"path": str(tmp_path / "cut.png")}
        Path(target["path"]).write_bytes(LEAKED.encode())
        await session.page._moved()
        raise asyncio.CancelledError

    manager.artifacts.capture_screenshot = AsyncMock(side_effect=cut_off)

    with pytest.raises(asyncio.CancelledError):
        await manager.observation.capture_session_screenshot(session, "observe")

    assert not list(tmp_path.glob("*.png")), "the refused page's frame was left in the artifact directory"


async def test_control_a_capture_cancelled_on_a_page_that_stayed_put_is_only_cancelled(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    session.page.move = None
    manager.artifacts.capture_screenshot = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await manager.observation.capture_session_screenshot(session, "observe")


async def test_a_screenshot_compare_uses_only_a_screenshot_a_checked_read_returned(tmp_path: Path) -> None:
    """A frame left on disk (a cancelled capture) was the newest PNG, and the compare served it."""
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    session.page.move = None
    png(tmp_path / STRAY)

    async def capture(_session, label: str, **kwargs) -> dict:
        target = kwargs.get("target") or {"path": str(tmp_path / f"{label}.png"), "url": f"/artifacts/s1/{label}.png"}
        png(Path(target["path"]))
        return dict(target)

    manager.artifacts.capture_screenshot = AsyncMock(side_effect=capture)
    manager.pii_scrubber.screenshot_enabled = False

    first = await manager.diagnostics.screenshot_diff("s1")
    assert first.get("baseline_captured") is True, first
    second = await manager.diagnostics.screenshot_diff("s1")

    assert STRAY not in str(first) + str(second), (first, second)
    assert second["a_url"] == "/artifacts/s1/screenshot-baseline.png", second


async def test_find_by_vision_uses_only_a_screenshot_a_checked_read_returned(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    session.page.move = None
    png(tmp_path / STRAY)
    targeter = SimpleNamespace(find_element=AsyncMock(return_value={"found": True}))
    gateway = McpToolGateway(
        manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), vision_targeter=targeter
    )
    payload = VisionFindInput(session_id="s1", description="submit", take_screenshot=False)

    with pytest.raises(RuntimeError, match="No screenshots available"):
        await gateway._find_by_vision(payload)
    targeter.find_element.assert_not_awaited()

    writes_a_screenshot(manager, session, tmp_path, moves=False)
    shot = await manager.observation.capture_session_screenshot(session, "observe")
    await gateway._find_by_vision(payload)
    assert targeter.find_element.await_args.args[0] == shot["path"]


async def test_control_a_read_that_raised_on_a_page_that_stayed_put_keeps_its_error(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    session.page.move = None

    with pytest.raises(PlaywrightError, match="selector never appeared"):
        async with page_read.locked_read(manager, session):
            raise PlaywrightError("selector never appeared")


@KINDS
async def test_the_dom_resource_is_refused_when_its_page_moved_while_it_was_read(tmp_path: Path, kind: str) -> None:
    manager = manager_for(tmp_path)
    read_session(manager, kind, tmp_path)
    transport = McpHttpTransport(
        tool_gateway=SimpleNamespace(list_tools=lambda: []),
        server_name="auto-browser",
        server_version="0",
        manager=manager,
    )

    resource = await transport._read_resource("browser://s1/dom")

    assert resource is None or LEAKED not in str(resource), resource


async def test_an_mcp_page_tool_gives_up_on_a_session_another_call_keeps(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(page_read, "READ_LOCK_TIMEOUT_SECONDS", QUICK_TIMEOUT)
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    session.page.move = None

    async with session.lock:
        with pytest.raises(RuntimeError, match="busy"):
            # Bounded here too, so a wait for the lock that never ends fails the test, not the run.
            await asyncio.wait_for(gateway_for(manager)._eval_js(EvalJsInput(session_id="s1", expression="1")), 2.0)


async def test_an_mcp_page_tool_gives_up_on_a_page_that_never_answers_and_lets_go_of_the_session(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(page_read, "PAGE_READ_TIMEOUT_SECONDS", QUICK_TIMEOUT)
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)

    async def never(*_args) -> None:
        await asyncio.sleep(HANG_SECONDS)

    # get_html, under the general bound: eval_js has its own timeout_ms (round 4, B2).
    session.page.content = never

    with pytest.raises(RuntimeError, match="did not answer"):
        await asyncio.wait_for(gateway_for(manager)._get_html(GetPageHtmlInput(session_id="s1")), 2.0)
    assert not session.lock.locked()


# ── REST CDP routes ──────────────────────────────────────────────────────────


class MovingCdp:
    def __init__(self, move) -> None:
        self.move = move
        self.page = None

    async def get_element_intelligence(self, selector: str) -> dict:
        self.move()
        return {"text": LEAKED}

    async def raw_cdp_command(self, method: str, params: dict) -> dict:
        self.move()
        return {"text": LEAKED}


@KINDS
@pytest.mark.parametrize("route", ["element", "raw"])
def test_a_cdp_route_is_refused_when_its_page_moved_while_it_was_read(tmp_path: Path, kind: str, route: str) -> None:
    """RD-02: the routes checked the page only before the call."""
    manager = manager_for(tmp_path)
    session = SimpleNamespace(id="s1", page=ReadPage(), lock=RecordingLock())
    manager.sessions["s1"] = session
    cdp = MovingCdp(lambda: MOVES[kind](manager, session, cdp.page))
    cdp.page = session.page
    app = FastAPI()
    app.state.browser_manager = manager
    app.state.cdp_sessions = {"s1": cdp}
    app.include_router(cdp_router)
    client = TestClient(app)

    if route == "element":
        response = client.get("/sessions/s1/cdp/element?selector=p")
    else:
        response = client.post("/sessions/s1/cdp/raw", json={"method": "DOM.getDocument", "params": {}})

    assert (response.status_code, response.json().get("detail")) == (403, OFF_LIST_PAGE_DETAIL)
    assert not session.lock.held


class FailingCdp(MovingCdp):
    """Moves the page, then fails: the route's error path had no check."""

    async def raw_cdp_command(self, method: str, params: dict) -> dict:
        self.move()
        raise RuntimeError(f"CDP failed: {LEAKED}")


def test_a_cdp_route_whose_page_moved_and_failed_answers_as_refused(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = SimpleNamespace(id="s1", page=ReadPage(), lock=RecordingLock())
    manager.sessions["s1"] = session
    cdp = FailingCdp(lambda: MOVES["returns"](manager, session, cdp.page))
    cdp.page = session.page
    app = FastAPI()
    app.state.browser_manager = manager
    app.state.cdp_sessions = {"s1": cdp}
    app.include_router(cdp_router)

    response = TestClient(app, raise_server_exceptions=False).post(
        "/sessions/s1/cdp/raw", json={"method": "DOM.getDocument", "params": {}}
    )

    assert response.status_code == 403, response.text
    assert response.json().get("detail") == OFF_LIST_PAGE_DETAIL
    assert not session.lock.held


# ── Refused URLs are shown by their origin (R-10) ────────────────────────────

# A refused page chooses its URL's path and query, which can carry what it read.
REFUSED_WITH_DATA = "https://user:pw@refused.example:8443/sso/cb?code=REFUSED-CONTENT#t"
REFUSED_ORIGIN = "https://refused.example:8443"


class RefusedPage:
    url = REFUSED_WITH_DATA

    def __init__(self) -> None:
        self.goto = AsyncMock()

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        return LEAKED


def refused_session(manager, tmp_path: Path) -> SimpleNamespace:
    session = read_session(manager, "returns", tmp_path)
    session.page = RefusedPage()
    session.context.pages = [session.page]
    return session


async def test_a_refused_tab_is_listed_by_its_origin(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = refused_session(manager, tmp_path)

    (tab,) = await manager.tabs.summaries(session)

    assert (tab["url"], tab["title"]) == (REFUSED_ORIGIN, "")


async def test_a_session_summary_shows_a_refused_page_by_its_origin(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = refused_session(manager, tmp_path)

    url, title, _ = await manager.session_lifecycle._page_snapshot(session)

    assert (url, title) == (REFUSED_ORIGIN, "")


class HangingPage:
    """Moves to a refused URL while its title is read, then never answers (a script in an endless loop)."""

    url = ALLOWED

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        self.url = REFUSED_WITH_DATA
        await asyncio.sleep(HANG_SECONDS)
        return LEAKED


async def test_a_session_summary_whose_title_read_times_out_shows_a_refused_page_by_its_origin(
    tmp_path: Path, monkeypatch
) -> None:
    """The title read is bounded; the URL returned when it gives up is the page's URL by then."""
    monkeypatch.setattr(sessions_module, "PAGE_TITLE_TIMEOUT_SECONDS", QUICK_TIMEOUT)
    manager = manager_for(tmp_path)
    session = refused_session(manager, tmp_path)
    session.page = HangingPage()
    session.context.pages = [session.page]

    url, title, live = await manager.session_lifecycle._page_snapshot(session)

    assert (url, title, live) == (REFUSED_ORIGIN, "", True)


async def test_an_action_snapshot_of_a_refused_page_shows_its_origin(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = refused_session(manager, tmp_path)

    snapshot = await manager.action_pipeline._failure_snapshot(
        SimpleNamespace(manager=manager, session=session), label="failed"
    )

    assert snapshot["url"] == REFUSED_ORIGIN, snapshot


async def test_an_approval_request_shows_a_refused_page_by_its_origin(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = refused_session(manager, tmp_path)
    manager._current_takeover_url = lambda _session: None
    manager.remote_access = SimpleNamespace(session_info=lambda _session: {})
    manager.session_lifecycle.isolation_payload = lambda _session: {}
    manager.auth_profiles.session_auth_state_info = lambda _session: {}

    observation = await manager.actions._approval_observation(session)

    assert (observation["url"], observation["title"]) == (REFUSED_ORIGIN, ""), observation


async def test_the_navigation_blocked_event_records_a_refused_page_by_its_origin(tmp_path: Path) -> None:
    """The audit log is read through /audit/events and browser://audit/events."""
    manager = manager_for(tmp_path)
    session = refused_session(manager, tmp_path)
    manager.audit.append = AsyncMock()

    await manager.diagnostics._return_to_blank(session, session.page)

    session.page.goto.assert_awaited_once_with("about:blank")
    assert manager.audit.append.await_args.kwargs["details"]["url"] == REFUSED_ORIGIN


async def test_a_saved_auth_profile_records_a_refused_page_by_its_origin(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = refused_session(manager, tmp_path)
    session.last_auth_state_path, session.auth_profile_name = None, None
    manager.auth_state.write_storage_state = AsyncMock(side_effect=write_empty_state)
    manager.auth_profiles.current_platform = lambda _session: "test"

    saved = await manager.auth_profiles.save_for_session(session, "p1")

    assert saved["metadata"]["saved_from_url"] == REFUSED_ORIGIN, saved["metadata"]


def browser_session(manager, page, tmp_path: Path) -> BrowserSession:
    """A real BrowserSession on a fake page: the action pipeline reads most of its fields."""
    for name in ("artifacts", "auth", "uploads"):
        (tmp_path / name).mkdir(exist_ok=True)
    session = BrowserSession(
        id="s1",
        name="t",
        created_at=datetime.now(UTC),
        context=SimpleNamespace(pages=[page]),
        page=page,
        artifact_dir=tmp_path / "artifacts",
        auth_dir=tmp_path / "auth",
        upload_dir=tmp_path / "uploads",
        takeover_url="",
        trace_path=tmp_path / "artifacts" / "trace.zip",
    )
    manager.sessions["s1"] = session
    manager.get_session = AsyncMock(return_value=session)
    return session


def pipeline_ready(manager) -> None:
    """The action pipeline with its records stubbed out, and no witness policy in the way."""
    manager.witness_policy.evaluate_action = lambda **_kwargs: SimpleNamespace(
        should_block=False, block_reason=None, require_approval=False
    )
    manager._ensure_witness_remote_ready = AsyncMock()
    manager.audit.append = AsyncMock()
    manager._append_jsonl = AsyncMock()
    manager._persist_session = AsyncMock()
    manager._record_witness_receipt = AsyncMock()
    manager.artifacts.trace_payload = lambda _session: {}


@pytest.mark.parametrize(
    ("action", "error"),
    [("click", "browser_action_blocked"), ("navigate", "browser_action_failed")],
    ids=["policy-block", "page-error"],
)
async def test_an_action_error_shows_a_refused_page_by_its_origin(tmp_path: Path, action: str, error: str) -> None:
    """The error's url: blocked while the page is refused (its blank did not take), or failed there."""
    manager = manager_for(tmp_path)
    session = browser_session(manager, RefusedPage(), tmp_path)
    pipeline_ready(manager)

    async def operation() -> None:
        raise PlaywrightError("Target page, context or browser has been closed")

    with pytest.raises(BrowserActionError) as failed:
        await manager.actions.run_action(session, action, {"selector": "#go"}, operation)

    assert failed.value.code == error
    assert failed.value.url == REFUSED_ORIGIN, failed.value.url


async def test_the_action_event_shows_a_page_refused_after_it_ran_by_its_origin(tmp_path: Path, monkeypatch) -> None:
    manager = manager_for(tmp_path)
    page = ReadPage()
    session = browser_session(manager, page, tmp_path)
    pipeline_ready(manager)
    manager._light_snapshot = AsyncMock(return_value={"url": ALLOWED, "title": ""})
    manager._observation_payload = AsyncMock(return_value={"url": ALLOWED})
    emitted: list[dict] = []
    monkeypatch.setattr("app.actions.pipeline._events.emit_action", lambda *_args: emitted.append(_args[3]))

    async def operation() -> None:
        # Lands on an allowed URL a refused page chose, still tainted (the opener being looked up).
        session.page.url = f"{ALLOWED}?d={LEAKED}"
        manager.page_provenance.opener_lookup_started(session.page)

    await manager.actions.run_action(session, "navigate", {"url": ALLOWED}, operation)

    assert emitted == [{"url": "https://allowed.example"}], emitted


async def test_control_an_allowed_tab_is_listed_by_its_whole_url(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    session.page.move = None
    session.page.url = f"{ALLOWED}?q=1"

    (tab,) = await manager.tabs.summaries(session)

    assert tab["url"] == f"{ALLOWED}?q=1"


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://user:pw@refused.example/p?q=1#f", "https://refused.example"),
        ("https://refused.example:8443/p", "https://refused.example:8443"),
        ("blob:https://refused.example:8443/9b1d", "https://refused.example:8443"),
        ("http://127.0.0.1:59456/x?y", "http://127.0.0.1:59456"),
        ("data:text/html,REFUSED-CONTENT", "data:"),
        ("blob:null/9b1d-REFUSED-CONTENT", "null"),
    ],
    ids=["userinfo", "port", "blob", "ip", "data", "opaque-blob"],
)
def test_url_origin_keeps_the_scheme_host_and_port_only(url: str, origin: str) -> None:
    """RD-10: a change that kept user:pass@ or a blob's path would have shipped green."""
    assert url_origin(url) == origin


async def test_a_tainted_blank_tab_is_listed_as_about_blank(tmp_path: Path) -> None:
    """The controller's blank carries nothing a page chose, so it is shown whole, not as "about:"."""
    manager = manager_for(tmp_path)
    session = read_session(manager, "blank", tmp_path)
    await session.page._moved()

    (tab,) = await manager.tabs.summaries(session)

    assert (tab["url"], tab["title"]) == ("about:blank", "")
