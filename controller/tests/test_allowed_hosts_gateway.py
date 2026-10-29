"""ALLOWED_HOSTS applies to what the MCP tools do to a session, not only to where its page goes.

set_cookies wrote cookies for any domain, so a caller confined to one host could
plant a session cookie for another and fixate it. And the tools that read the page
returned whatever it showed, even after the page had moved itself onto a host the
list refuses. No browser is needed here: the gateway runs against a real
BrowserManager's allowlist and a mocked context, or a mocked page.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as PlaywrightError

from app.browser_manager import BrowserManager
from app.tool_gateway import McpToolGateway
from app.tool_gateway import gateway as gateway_module
from app.tool_inputs import (
    DragDropInput,
    EvalJsInput,
    FindElementsInput,
    GetPageHtmlInput,
    GetStorageInput,
    SetCookiesInput,
    SetStorageInput,
    WaitForSelectorInput,
)
from tests.test_allowed_hosts_reads import gateway_for, manager_for, read_session
from tests.test_browser_manager_create_session import _settings


def _gateway(tmp_path: Path, allowed_hosts: str) -> tuple[McpToolGateway, AsyncMock]:
    settings = _settings(tmp_path)
    settings.allowed_hosts = allowed_hosts
    manager = BrowserManager(settings)
    context = SimpleNamespace(add_cookies=AsyncMock())
    manager.get_session = AsyncMock(return_value=SimpleNamespace(context=context))  # type: ignore[method-assign]
    gateway = McpToolGateway(manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())
    return gateway, context.add_cookies


def _cookies(*cookies: dict) -> SetCookiesInput:
    return SetCookiesInput(session_id="s1", cookies=list(cookies))


ALLOWED_COOKIE = {"name": "a", "value": "1", "url": "https://allowed.example/"}


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "b", "value": "2", "url": "https://evil.example/"},
        {"name": "b", "value": "2", "domain": "evil.example", "path": "/"},
        {"name": "b", "value": "2", "domain": ".evil.example", "path": "/"},
    ],
)
async def test_set_cookies_refuses_a_cookie_for_a_disallowed_host_and_adds_none(tmp_path: Path, bad: dict) -> None:
    gateway, add_cookies = _gateway(tmp_path, "allowed.example")

    with pytest.raises(PermissionError, match="evil.example"):
        await gateway._set_cookies(_cookies(ALLOWED_COOKIE, bad))

    add_cookies.assert_not_called()


async def test_set_cookies_accepts_cookies_for_allowed_hosts(tmp_path: Path) -> None:
    gateway, add_cookies = _gateway(tmp_path, "allowed.example")
    by_domain = {"name": "b", "value": "2", "domain": ".allowed.example", "path": "/"}

    result = await gateway._set_cookies(_cookies(ALLOWED_COOKIE, by_domain))

    assert result == {"session_id": "s1", "set": 2}
    add_cookies.assert_awaited_once_with([ALLOWED_COOKIE, by_domain])


async def test_set_cookies_is_unrestricted_when_allowed_hosts_is_a_wildcard(tmp_path: Path) -> None:
    gateway, add_cookies = _gateway(tmp_path, "*")
    anywhere = {"name": "b", "value": "2", "domain": "evil.example", "path": "/"}

    await gateway._set_cookies(_cookies(anywhere))

    add_cookies.assert_awaited_once()


# ── the page-content tools check the page before they read it ────────────────
# Real-browser coverage is in test_allowed_hosts_navigation.py; this runs without Chromium.


def _page_tool_gateway() -> tuple[McpToolGateway, SimpleNamespace]:
    page = SimpleNamespace(
        evaluate=AsyncMock(),
        content=AsyncMock(),
        wait_for_selector=AsyncMock(),
        locator=lambda selector: SimpleNamespace(first=SimpleNamespace(bounding_box=AsyncMock())),
        mouse=SimpleNamespace(move=AsyncMock(), down=AsyncMock(), up=AsyncMock()),
    )
    refusal = PermissionError("The page navigated on its own to a host ALLOWED_HOSTS does not allow")
    manager = SimpleNamespace(
        get_session=AsyncMock(return_value=SimpleNamespace(page=page)),
        _assert_page_allowed=Mock(side_effect=refusal),
    )
    gateway = McpToolGateway(manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())
    return gateway, page


PAGE_TOOLS = {
    "eval_js": ("_eval_js", EvalJsInput(session_id="s1", expression="1")),
    "wait_for_selector": ("_wait_for_selector", WaitForSelectorInput(session_id="s1", selector="p")),
    "get_html": ("_get_html", GetPageHtmlInput(session_id="s1")),
    "get_text": ("_get_html", GetPageHtmlInput(session_id="s1", text_only=True)),
    "find_by_query": ("_find_elements", FindElementsInput(session_id="s1", query="x")),
    "find_by_selector": ("_find_elements", FindElementsInput(session_id="s1", selector="p")),
    "drag_drop": ("_drag_drop", DragDropInput(session_id="s1", source_selector="a", target_selector="b")),
    "get_storage": ("_get_local_storage", GetStorageInput(session_id="s1")),
    "set_storage": ("_set_local_storage", SetStorageInput(session_id="s1", key="k", value="v")),
}


@pytest.mark.parametrize("tool", sorted(PAGE_TOOLS))
async def test_page_tools_refuse_before_touching_the_page(tool: str) -> None:
    gateway, page = _page_tool_gateway()
    handler, payload = PAGE_TOOLS[tool]

    with pytest.raises(PermissionError, match="ALLOWED_HOSTS"):
        await getattr(gateway, handler)(payload)

    page.evaluate.assert_not_called()
    page.content.assert_not_called()
    page.wait_for_selector.assert_not_called()
    page.mouse.move.assert_not_called()


# ── wait_for_selector waits without the session lock (round 4, B1) ───────────

# How long an action may wait for the session while wait_for_selector waits.
ACTION_WAIT_SECONDS = 1.0


async def test_an_action_runs_on_the_session_while_wait_for_selector_waits(tmp_path: Path) -> None:
    """The wait lasts up to its timeout (60 s): under the session lock it stalled every action on the
    session meanwhile, the click that makes the element appear among them."""
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    session.page.move = None
    appeared, entered = asyncio.Event(), asyncio.Event()

    async def wait_for_selector(*_args, **_kwargs) -> None:
        entered.set()
        await appeared.wait()

    session.page.wait_for_selector = wait_for_selector
    waiting = asyncio.ensure_future(
        gateway_for(manager)._wait_for_selector(WaitForSelectorInput(session_id="s1", selector="#late"))
    )
    # The wait is under way (past any lock it might take) before the action asks for the session (F7).
    await asyncio.wait_for(entered.wait(), timeout=ACTION_WAIT_SECONDS)

    async def click() -> None:
        async with session.lock:  # what every action holds while it runs (BrowserActionPipeline.run)
            appeared.set()

    action = asyncio.ensure_future(click())
    done, _ = await asyncio.wait({action}, timeout=ACTION_WAIT_SECONDS)
    if not done:
        action.cancel()
        waiting.cancel()
    assert done, "the action could not run until the wait had ended"

    assert (await waiting)["selector"] == "#late"


# ── A wait refused because its page moved answers at its timeout (round 4c, CR-3) ──

WAIT_TIMEOUT_MS = 400
# asyncio may wake a sleeper up to its clock's resolution early (about 16 ms on Windows).
CLOCK_TOLERANCE_SECONDS = 0.05


async def timed_wait(manager, session, wait_for_selector) -> float:
    """Seconds the wait took to answer; it must answer with the moved-page refusal."""
    session.page.wait_for_selector = wait_for_selector
    started = time.monotonic()
    with pytest.raises(PermissionError, match="moved while it was read"):
        await gateway_for(manager)._wait_for_selector(
            WaitForSelectorInput(session_id="s1", selector="#x", timeout_ms=WAIT_TIMEOUT_MS)
        )
    return time.monotonic() - started


@pytest.mark.parametrize("raises", [False, True], ids=["matched", "raised"])
async def test_a_wait_refused_because_its_page_moved_answers_no_sooner_than_its_timeout(
    tmp_path: Path, raises: bool
) -> None:
    """A refusal right after the selector matched on the refused document, against one at the timeout when it
    did not, told the agent whether its selector matched there: one bit per call, from the latency."""
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)

    async def wait_for_selector(*_args, **_kwargs) -> None:
        await session.page._moved()
        if raises:
            raise PlaywrightError("Unexpected token in selector")

    elapsed = await timed_wait(manager, session, wait_for_selector)

    assert elapsed >= WAIT_TIMEOUT_MS / 1000 - CLOCK_TOLERANCE_SECONDS, elapsed


# The pad a moved wait's refusal waits past its timeout (gateway.MOVED_PAD_SECONDS), set for these tests,
# and how much later than that an answer still counts as the same time.
MOVED_PAD_SECONDS = 0.25
PAD_SLACK_SECONDS = 0.15
# How late a fake Playwright answers an unmatched wait: its own timer, then one IPC round trip.
IPC_LAG_SECONDS = 0.03
# How long a test lets a moved wait run into its refusal's sleep before cancelling it.
INTO_THE_SLEEP_SECONDS = 0.05


@pytest.mark.parametrize("matched", [True, False], ids=["matched", "unmatched-answered-late"])
async def test_a_moved_wait_answers_at_its_timeout_and_pad_whether_or_not_the_selector_matched(
    tmp_path: Path, monkeypatch, matched: bool
) -> None:
    """Round 4d (R4C-L1-8): a matched wait was refused exactly at the deadline, and an unmatched one at
    Playwright's own timer plus an IPC round trip, so on a fine clock an agent timing many refusals could
    tell the two apart."""
    monkeypatch.setattr(gateway_module, "MOVED_PAD_SECONDS", MOVED_PAD_SECONDS, raising=False)
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)

    async def wait_for_selector(*_args, timeout: float, **_kwargs) -> None:
        await session.page._moved()
        if not matched:
            await asyncio.sleep(timeout / 1000 + IPC_LAG_SECONDS)
            raise PlaywrightError(f"Timeout {timeout}ms exceeded.")

    elapsed = await timed_wait(manager, session, wait_for_selector)

    answer_at = WAIT_TIMEOUT_MS / 1000 + MOVED_PAD_SECONDS
    assert answer_at - CLOCK_TOLERANCE_SECONDS <= elapsed < answer_at + PAD_SLACK_SECONDS, elapsed


def chained(exc: BaseException | None) -> list[BaseException]:
    """The exception and every one its __cause__ or __context__ leads to, suppressed or not."""
    found: list[BaseException] = []
    while exc is not None and all(exc is not seen for seen in found):
        found.append(exc)
        exc = exc.__cause__ or exc.__context__
    return found


@pytest.mark.parametrize("cancelled", [False, True], ids=["refused", "cancelled-while-it-waits"])
async def test_a_moved_waits_answer_keeps_nothing_of_the_waits_own_error(
    tmp_path: Path, monkeypatch, cancelled: bool
) -> None:
    """Round 4d (R4C-L1-8): the refusal waited inside the wait's except block, so its PermissionError, and a
    cancel during that sleep, carried Playwright's error as __context__. Its call log can quote the refused
    page's markup, and a cancel logged with its traceback shows it."""
    monkeypatch.setattr(gateway_module, "MOVED_PAD_SECONDS", MOVED_PAD_SECONDS, raising=False)
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    failed = asyncio.Event()

    async def wait_for_selector(*_args, **_kwargs) -> None:
        await session.page._moved()
        failed.set()
        raise PlaywrightError("waiting for locator('#x') <div>REFUSED-MARKUP</div>")

    session.page.wait_for_selector = wait_for_selector
    wait = asyncio.ensure_future(
        gateway_for(manager)._wait_for_selector(
            WaitForSelectorInput(session_id="s1", selector="#x", timeout_ms=WAIT_TIMEOUT_MS)
        )
    )
    if cancelled:
        await asyncio.wait_for(failed.wait(), timeout=WAIT_TIMEOUT_MS / 1000)
        await asyncio.sleep(INTO_THE_SLEEP_SECONDS)
        wait.cancel()

    with pytest.raises(asyncio.CancelledError if cancelled else PermissionError) as answered:
        await wait

    assert not [exc for exc in chained(answered.value) if isinstance(exc, PlaywrightError)], chained(answered.value)


async def test_control_a_wait_whose_page_did_not_move_answers_when_the_selector_matches(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    session.page.move = None
    session.page.wait_for_selector = AsyncMock()

    started = time.monotonic()
    await gateway_for(manager)._wait_for_selector(
        WaitForSelectorInput(session_id="s1", selector="#x", timeout_ms=WAIT_TIMEOUT_MS)
    )

    assert time.monotonic() - started < WAIT_TIMEOUT_MS / 1000 / 2
