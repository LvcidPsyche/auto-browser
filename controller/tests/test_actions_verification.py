"""An action that opens a tab is verified by it, and its result names the tab (NAV-08).

Observations and actions stay on the active tab, and the controller does not move to a tab a page opens. A click on a
link that opens a new tab changed nothing on the active page, so its verification could read ``verified: false``, and
an agent that retried opened a second tab. The before snapshot now counts the tabs, the verification reports
``tabs_changed``, which verifies a click or a key press, and the result names the tab that opened.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.browser.services.actions import NEW_TAB_MESSAGE, BrowserActionService
from app.browser_manager import BrowserManager, BrowserSession
from app.utils import UTC
from tests.test_browser_manager_actions import FakeContext, FakePage, _settings

URL = "https://example.com/"
PAGE = {
    "url": URL,
    "title": "t",
    "active_element": None,
    "text_excerpt": "unchanged",
    "dom_outline": {},
    "accessibility_outline": {},
}


def _tabs(count: int, *, active: int = 0) -> list[dict]:
    return [{"index": index, "active": index == active, "url": URL, "title": "t"} for index in range(count)]


def _verify(action: str, before_tabs: int | None, after_tabs: int | None) -> dict:
    before = dict(PAGE) if before_tabs is None else {**PAGE, "tab_count": before_tabs}
    after = dict(PAGE) if after_tabs is None else {**PAGE, "tabs": _tabs(after_tabs)}
    return BrowserActionService.action_verification(action, {}, before, after)


def _session(root: Path, page: FakePage, context: FakeContext) -> BrowserSession:
    dirs = {name: root / name / "s1" for name in ("artifacts", "auth", "uploads")}
    for folder in dirs.values():
        folder.mkdir(parents=True, exist_ok=True)
    return BrowserSession(
        id="s1",
        name="fake",
        created_at=datetime.now(UTC),
        context=context,
        page=page,
        artifact_dir=dirs["artifacts"],
        auth_dir=dirs["auth"],
        upload_dir=dirs["uploads"],
        takeover_url="http://x",
        trace_path=dirs["artifacts"] / "trace.zip",
    )


@pytest.mark.parametrize("action", ["click", "press"])
def test_a_click_or_press_that_opened_a_tab_is_verified_by_it(action: str) -> None:
    verification = _verify(action, 1, 2)

    assert verification["signals"] == ["tabs_changed"]
    assert verification["verified"] is True


def test_a_click_that_closed_a_tab_is_verified_by_it_too() -> None:
    verification = _verify("click", 2, 1)

    assert verification["signals"] == ["tabs_changed"]
    assert verification["verified"] is True


def test_the_same_tab_count_is_no_signal() -> None:
    verification = _verify("click", 2, 2)

    assert verification["signals"] == []
    assert verification["verified"] is False


@pytest.mark.parametrize(("before_tabs", "after_tabs"), [(None, 2), (1, None)])
def test_a_snapshot_that_did_not_see_the_tabs_gives_no_tab_signal(before_tabs, after_tabs) -> None:
    # An observation that failed after the action is the URL and an error; an older snapshot has no count.
    assert "tabs_changed" not in _verify("click", before_tabs, after_tabs)["signals"]


@pytest.mark.parametrize("action", ["type", "scroll"])
def test_a_new_tab_alone_does_not_verify_typing_or_a_scroll(action: str) -> None:
    verification = _verify(action, 1, 2)

    assert verification["signals"] == ["tabs_changed"]
    assert verification["verified"] is False


@pytest.mark.parametrize("action", ["wait", "reload"])
def test_a_wait_or_a_reload_reports_nothing_verified(action: str) -> None:
    # ACT-30: both read verified: true whatever the page did. A wait has nothing to verify, and a reload of a page
    # that did not change leaves nothing to compare; the signals are still reported.
    unchanged = BrowserActionService.action_verification(action, {}, PAGE, PAGE)
    changed = BrowserActionService.action_verification(action, {}, PAGE, {**PAGE, "text_excerpt": "new"})

    assert unchanged == {"verified": None, "signals": [], "target_seen_after": None}
    assert changed == {"verified": None, "signals": ["text_excerpt_changed"], "target_seen_after": None}


@pytest.mark.parametrize("action", ["navigate", "go_back", "go_forward"])
@pytest.mark.parametrize(
    ("after", "verified"),
    [
        ({**PAGE, "text_excerpt": "new"}, False),
        ({**PAGE, "active_element": "input#q"}, False),
        ({**PAGE, "url": "https://example.com/next"}, True),
        ({**PAGE, "title": "Next"}, True),
    ],
    ids=["text-only", "focus-only", "url", "title"],
)
def test_a_navigation_is_verified_by_its_url_or_title_alone(action: str, after: dict, verified: bool) -> None:
    # No test held the rule: a navigation given the default (any signal) passed every file, and so did a go_back or
    # a go_forward. A page whose text or focus changed while its address and title did not was not navigated.
    assert BrowserActionService.action_verification(action, {}, PAGE, after)["verified"] is verified


async def test_the_action_snapshot_counts_the_sessions_tabs(tmp_path: Path) -> None:
    manager = BrowserManager(_settings(tmp_path))
    manager.settings.pii_scrub_action_screenshots = False
    page = FakePage(URL)
    context = FakeContext(page)
    await context.new_page()
    session = _session(tmp_path, page, context)
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"PNG")
    manager._capture_screenshot = AsyncMock(return_value={"path": str(shot), "url": "/artifacts/s1/shot.png"})
    manager.observation.page_summary = AsyncMock(return_value={key: PAGE[key] for key in PAGE if key != "url"})

    snapshot = await manager.observation.light_snapshot(session, label="before-click")

    assert snapshot["tab_count"] == 2


@pytest.fixture
def action_manager(tmp_path: Path):
    manager = BrowserManager(_settings(tmp_path))
    manager.audit.append = AsyncMock()
    manager._append_jsonl = AsyncMock()
    manager._persist_session = AsyncMock()
    manager._record_witness_receipt = AsyncMock()
    manager._settle = AsyncMock()
    manager._ensure_witness_remote_ready = AsyncMock()
    manager._check_bot_challenge = AsyncMock(return_value=None)
    manager._light_snapshot = AsyncMock(return_value={**PAGE, "tab_count": 1})
    page = FakePage(URL)
    session = _session(tmp_path, page, FakeContext(page))
    manager.sessions["s1"] = session
    return manager, session


async def _click(manager: BrowserManager, session: BrowserSession, after_tabs: list[dict]) -> dict:
    manager._observation_payload = AsyncMock(return_value={**PAGE, "tabs": after_tabs})
    return await manager._run_action(session, "click", {"selector": "#open"}, AsyncMock())


async def test_a_click_that_opened_a_tab_names_it_in_its_result(action_manager) -> None:
    result = await _click(*action_manager, _tabs(2))

    assert result["new_tab"] == {"index": 1, "message": NEW_TAB_MESSAGE}
    assert result["verification"]["verified"] is True


async def test_a_click_that_opened_no_tab_has_no_tab_hint(action_manager) -> None:
    result = await _click(*action_manager, _tabs(1))

    assert "new_tab" not in result


async def test_a_new_tab_that_is_already_the_active_one_has_no_hint(action_manager) -> None:
    # The hint says the tab is not the active one; the session moves only when its own tab closes.
    result = await _click(*action_manager, _tabs(2, active=1))

    assert "new_tab" not in result
