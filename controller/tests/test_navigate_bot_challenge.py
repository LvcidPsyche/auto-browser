"""A navigation is probed for a bot challenge once, and one that lands on a challenge asks for one takeover.

navigate's own operation probed the page (BrowserActionService.navigate) and, on a challenge, requested a takeover;
the pipeline then probed the page again, as after every action, and requested a second takeover before it failed the
action with captcha_detected: every takeover record twice, and a challenge that passed between the two probes left a
takeover asked for under a navigation reported done. These run the real navigate through the real pipeline; only the
page, the snapshots and the record writers are fakes. The challenge is read by the real probe
(BrowserManager._check_bot_challenge) from the page's title. test_navigate_bot_challenge_chromium.py runs the same in
Chromium with the real records.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.action_errors import BrowserActionError
from app.browser.services.bot_challenge import CHALLENGE_PROBE_SCRIPT
from app.browser_manager import BrowserManager
from tests.test_actions_verification import PAGE, URL, _session
from tests.test_browser_manager_actions import FakeContext, FakePage, _settings

CHALLENGE_TITLE = "Just a moment..."  # Cloudflare's interstitial, as the probe reads it


class ProbedPage(FakePage):
    """A page whose challenge probe reads ``shown_title``: Cloudflare's interstitial, or an ordinary page."""

    def __init__(self, url: str, title: str) -> None:
        super().__init__(url)
        self.probes = 0
        self.shown_title = title

    async def evaluate(self, script: str, *_args: Any, **_kwargs: Any) -> Any:
        assert script == CHALLENGE_PROBE_SCRIPT, script[:80]
        self.probes += 1
        return {"title": self.shown_title, "text": "", "iframes": []}


def _manager(tmp_path: Path, title: str) -> tuple[BrowserManager, ProbedPage]:
    manager = BrowserManager(_settings(tmp_path))
    manager.audit.append = AsyncMock()
    manager._append_jsonl = AsyncMock()
    manager._persist_session = AsyncMock()
    manager._record_witness_receipt = AsyncMock()
    manager._settle = AsyncMock()
    manager._ensure_witness_remote_ready = AsyncMock()
    manager._light_snapshot = AsyncMock(return_value={**PAGE, "tab_count": 1})
    manager._observation_payload = AsyncMock(return_value=PAGE)
    page = ProbedPage("about:blank", title)
    manager.sessions["s1"] = _session(tmp_path, page, FakeContext(page))
    return manager, page


def _takeovers(manager: BrowserManager) -> list[dict[str, Any]]:
    events = manager.audit.append.await_args_list
    return [call.kwargs for call in events if call.kwargs["event_type"] == "takeover_requested"]


async def test_a_navigation_onto_a_challenge_requests_one_takeover(tmp_path: Path) -> None:
    manager, page = _manager(tmp_path, CHALLENGE_TITLE)

    with pytest.raises(BrowserActionError) as failed:
        await manager.navigate("s1", URL)

    assert failed.value.code == "captcha_detected"
    receipts = manager._record_witness_receipt.await_args_list
    lines = manager._append_jsonl.await_args_list
    assert [takeover["details"] for takeover in _takeovers(manager)] == [
        {"reason": "Bot challenge detected: challenge page title"}
    ]
    assert len([call for call in receipts if call.kwargs["action"] == "request_human_takeover"]) == 1
    assert len([call for call in lines if call.args[1].get("action") == "request_human_takeover"]) == 1
    assert page.probes == 1


async def test_a_navigation_reported_done_has_asked_for_no_takeover(tmp_path: Path) -> None:
    # Cloudflare's interstitial passes by itself: shown when the navigation's settle ends, gone by the time the
    # pipeline has checked the URL and run the TOTP step (here the step is where it goes).
    manager, page = _manager(tmp_path, CHALLENGE_TITLE)

    async def interstitial_passes(_session: Any) -> None:
        page.shown_title = "Example Domain"

    manager._maybe_handle_totp = interstitial_passes

    result = await manager.navigate("s1", URL)

    assert result["action"] == "navigate"
    assert _takeovers(manager) == []


async def test_an_ordinary_navigation_probes_the_page_once(tmp_path: Path) -> None:
    manager, page = _manager(tmp_path, "Example Domain")

    result = await manager.navigate("s1", URL)

    assert result["action"] == "navigate"
    assert _takeovers(manager) == []
    assert page.probes == 1
