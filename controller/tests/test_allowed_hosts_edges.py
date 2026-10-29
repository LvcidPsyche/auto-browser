"""ALLOWED_HOSTS at the edges (round 4): a screenshot cut off by cancellation, fork_session, IPv6 partition keys.

Fakes stand in for the browser, so each case happens in the same order on every run.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.action_errors import RefusedPageError
from app.browser.page_provenance import CLOSED_PAGE_GRACE_SECONDS
from app.models import McpToolCallRequest
from app.routes._utils import OFF_LIST_PAGE_DETAIL
from app.routes.sessions import create_sessions_router
from app.tool_gateway import McpToolGateway
from app.tool_inputs import GetCookiesInput
from tests.test_allowed_hosts_reads import full_gateway
from tests.test_allowed_hosts_rechecks import ALLOWED, REFUSED, MovingPage, manager_for, tab_session
from tests.test_auth_state_ownership import StateContext, build_manager

# Item 27: a tab is refused for another tab also when its navigation came just after a refused tab closed.
CLOSED_TAB_CLAUSE = f"within {CLOSED_PAGE_GRACE_SECONDS:g} seconds after a refused tab closed"
# Item 36: closing the refused tab does not clear this one (only the agent's navigate does, and a tab that is not
# active cannot be navigated), so the advice is REFUSED_TAB_MESSAGE's.
TAB_ADVICE = "and cannot be activated. Close it, or open a new tab."
# Item 51: the active page can be navigated, but the refused tab may have closed already.
PAGE_ADVICE = "close the refused tab if it is still open, then navigate this one"

# How long a test waits for a capture thread and the loop callbacks after it.
SETTLE_SECONDS = 2.0


# ── A screenshot capture cut off by cancellation (A2 item 10) ──


async def test_a_screenshot_cut_off_by_cancellation_leaves_no_file(tmp_path: Path) -> None:
    """Playwright writes the image in a thread once it arrives (async_writefile): cancelling the read does
    not stop that write, which then left an unchecked file behind."""
    manager = manager_for(tmp_path)
    session = tab_session(MovingPage(ALLOWED, ALLOWED))
    session.artifact_dir = tmp_path
    started, release, written = asyncio.Event(), threading.Event(), threading.Event()
    paths: list[Path] = []

    async def capture(_session, _label: str, *, target: dict[str, str]) -> dict[str, str]:
        path = Path(target["path"])
        paths.append(path)

        def write() -> None:
            release.wait(SETTLE_SECONDS)
            path.write_bytes(b"UNCHECKED-PIXELS")
            written.set()

        started.set()
        await asyncio.get_running_loop().run_in_executor(None, write)
        return dict(target)

    manager.artifacts.capture_screenshot = capture
    read = asyncio.ensure_future(manager.observation.capture_session_screenshot(session, "shot"))
    await started.wait()

    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read
    release.set()
    for _ in range(int(SETTLE_SECONDS / 0.02)):
        if written.is_set() and not paths[0].exists():
            break
        await asyncio.sleep(0.02)

    assert written.is_set()
    assert not paths[0].exists()


# ── fork_session refuses a refused page (A2 item 11) ──


async def fork_source(tmp_path: Path, url: str):
    """A manager with one live session whose page is on `url`, and that session's id."""
    manager, _ = build_manager(tmp_path)
    manager.settings.allowed_hosts = "allowed.example"
    manager.settings.max_sessions = 5
    manager.audit.append = AsyncMock()
    session_id = (await manager.create_session(name="source"))["id"]
    session = manager.sessions[session_id]
    session.context = StateContext(session.page)
    session.page.url = url
    return manager, session_id


async def test_a_tainted_page_on_an_allowed_url_is_not_forked(tmp_path: Path) -> None:
    """The fork opened the page's URL (the refused page's choice) with its cookies, and audited it whole."""
    manager, session_id = await fork_source(tmp_path, f"{ALLOWED}?d=REFUSED-CONTENT")
    manager.page_provenance.committed(manager.sessions[session_id].page, REFUSED, url_refused=True)

    with pytest.raises(PermissionError):
        await manager.fork_session(session_id)

    assert list(manager.sessions) == [session_id]
    assert "REFUSED-CONTENT" not in str(manager.audit.append.await_args_list)
    assert not list((tmp_path / "auth").rglob("fork_*.json"))


async def test_control_an_allowed_page_is_forked_and_its_url_audited(tmp_path: Path) -> None:
    url = f"{ALLOWED}?step=2"
    manager, session_id = await fork_source(tmp_path, url)

    forked = await manager.fork_session(session_id)

    assert manager.sessions[forked["id"]].page.url == url
    assert manager.audit.append.await_args.kwargs["details"]["start_url"] == url


async def test_a_tainted_page_is_forked_onto_a_start_url_the_caller_names(tmp_path: Path) -> None:
    """F4: the page's URL is not used then, and create_session judges the named one; the fork refused anyway."""
    manager, session_id = await fork_source(tmp_path, f"{ALLOWED}?d=REFUSED-CONTENT")
    manager.page_provenance.committed(manager.sessions[session_id].page, REFUSED, url_refused=True)

    forked = await manager.fork_session(session_id, start_url=f"{ALLOWED}/fresh")

    assert manager.sessions[forked["id"]].page.url == f"{ALLOWED}/fresh"
    assert "REFUSED-CONTENT" not in str(manager.audit.append.await_args_list)


def page_refusal_type(manager) -> type[PermissionError]:
    """The type of the refusal the page checks raise for a page ALLOWED_HOSTS refuses."""
    try:
        manager._assert_page_object_allowed(SimpleNamespace(url=REFUSED))
    except PermissionError as exc:
        return type(exc)
    raise AssertionError("a page on a refused host was not refused")


async def test_a_fork_of_a_page_on_about_blank_is_not_refused_as_a_refused_page(tmp_path: Path) -> None:
    """Round 4d (R4C-L1-5, T7): a fresh session still on about:blank is refused by create_session's URL check
    (no about: start URL), and REST answered that the page was on, or came from, a refused host, because every
    PermissionError of a fork without start_url was taken for the page's."""
    manager, session_id = await fork_source(tmp_path, "about:blank")

    with pytest.raises(PermissionError, match="scheme") as refused:
        await manager.fork_session(session_id)

    assert not isinstance(refused.value, page_refusal_type(manager))


async def test_control_a_start_url_off_the_list_is_still_refused(tmp_path: Path) -> None:
    manager, session_id = await fork_source(tmp_path, f"{ALLOWED}/")

    with pytest.raises(PermissionError):
        await manager.fork_session(session_id, start_url=REFUSED)

    assert list(manager.sessions) == [session_id]


# ── IPv6 partition keys (A2 item 12) ──


async def test_a_cookie_partitioned_under_a_listed_ipv6_host_is_returned(tmp_path: Path) -> None:
    """The key's host was put into a URL unbracketed (https://::1/), which names no host, so it was refused."""
    manager = manager_for(tmp_path)
    manager.settings.allowed_hosts = "::1"
    jar = [
        {"name": "under-listed", "domain": "[::1]", "partitionKey": "https://[::1]"},
        {"name": "under-unlisted", "domain": "[::1]", "partitionKey": "https://[::2]"},
    ]
    context = SimpleNamespace(cookies=AsyncMock(return_value=jar))
    manager.get_session = AsyncMock(return_value=SimpleNamespace(context=context))
    gateway = McpToolGateway(manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())

    result = await gateway._get_cookies(GetCookiesInput(session_id="s1"))

    assert [c["name"] for c in result["cookies"]] == ["under-listed"]


# ── A refused tab that is not the active one (round 4, after D4) ──


async def test_activating_a_refused_tab_over_mcp_says_to_close_it(tmp_path: Path) -> None:
    """Only the active page can be navigated, so "until it is navigated to an allowed URL" did not apply to
    a tab that cannot be activated; the agent had no way out but closing it, and was not told so."""
    manager = manager_for(tmp_path)
    active, tainted = MovingPage(ALLOWED, ALLOWED), MovingPage(ALLOWED, ALLOWED)
    session = tab_session(active)
    session.context.pages.append(tainted)
    session.lock = asyncio.Lock()
    manager.get_session = AsyncMock(return_value=session)
    manager.page_provenance.committed(tainted, REFUSED, url_refused=True)

    response = await full_gateway(manager)._call_tool(
        McpToolCallRequest(name="browser.activate_tab", arguments={"session_id": "s1", "index": 1})
    )

    text = response.content[0].text
    assert response.isError and "cannot be activated" in text and "Close it" in text, text
    assert "navigated to an allowed URL" not in text
    assert session.page is active
    # The active page's own refusal still says how to go on.
    with pytest.raises(PermissionError, match="until it is navigated to an allowed URL"):
        manager._assert_page_object_allowed(tainted)


def refused_by_another_tab(manager):
    """A session whose second tab, on an allowed page, is refused only because another tab was refused when it loaded."""
    active, other = MovingPage(ALLOWED, ALLOWED), MovingPage(ALLOWED, ALLOWED)
    session = tab_session(active)
    session.context.pages.append(other)
    session.lock = asyncio.Lock()
    manager.get_session = AsyncMock(return_value=session)
    manager.page_provenance.committed(other, ALLOWED, url_refused=False, by_another_page=True)
    return session, active


async def test_activating_a_tab_refused_only_by_another_tab_over_mcp_names_the_cause(tmp_path: Path) -> None:
    """F13: the refusal blamed the tab's own host ("on a host ALLOWED_HOSTS does not allow, or came from
    one"), though the tab is on an allowed page and the way out is closing the tab that was refused."""
    manager = manager_for(tmp_path)
    session, active = refused_by_another_tab(manager)

    response = await full_gateway(manager)._call_tool(
        McpToolCallRequest(name="browser.activate_tab", arguments={"session_id": "s1", "index": 1})
    )

    text = response.content[0].text
    assert response.isError and "another tab of this session was refused" in text, text
    assert CLOSED_TAB_CLAUSE in text, text  # item 27: the grace after a refused tab closed counts too
    assert text.endswith(TAB_ADVICE) and "does not allow" not in text, text
    assert session.page is active


def test_activating_a_tab_refused_only_by_another_tab_over_rest_names_the_cause(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    refused_by_another_tab(manager)
    app = FastAPI()
    app.include_router(create_sessions_router(manager=manager))

    response = TestClient(app, raise_server_exceptions=False).post("/sessions/s1/tabs/activate", json={"index": 1})

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert "another tab of this session was refused" in detail and "does not allow" not in detail, detail
    assert CLOSED_TAB_CLAUSE in detail, detail
    assert detail.endswith(TAB_ADVICE), detail


def test_the_active_pages_refusal_by_another_tab_names_the_grace_and_a_tab_that_may_be_gone(tmp_path: Path) -> None:
    """Item 51 (Fable E-NAV-5): the active page's refusal, and the routes' fixed text, said only "while another
    tab of this session was refused" and "close the refused tab", though within the grace after it closed the
    refused tab is already gone."""
    manager = manager_for(tmp_path)
    page = MovingPage(ALLOWED, ALLOWED)
    manager.page_provenance.committed(page, ALLOWED, url_refused=False, by_another_page=True)

    with pytest.raises(RefusedPageError) as refused:
        manager._assert_page_object_allowed(page)

    for text in (str(refused.value), OFF_LIST_PAGE_DETAIL):
        assert "another tab of this session was refused" in text, text
        assert CLOSED_TAB_CLAUSE in text, text
        assert PAGE_ADVICE in text.lower(), text
