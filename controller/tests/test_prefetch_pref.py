"""app/browser/prefetch.py on a fake browser: idempotent, bounded, never raising, and the settings tab always closed.

The real Chromium route is tests/test_request_gate_hardening_chromium.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.browser import prefetch
from tests._fake_cdp import FakeCDPSession

pytestmark = pytest.mark.asyncio


class SettingsPage:
    """The chrome://settings tab: answers Runtime.evaluate through Target.receivedMessageFromTarget."""

    def __init__(self, cdp: FakeCDPSession, *, pref: object = 0, answers: bool = True) -> None:
        self.cdp, self.pref, self.answers = cdp, pref, answers
        self.expressions: list[str] = []

    def __call__(self, params: dict) -> dict:
        message = json.loads(params["message"])
        expression = message["params"]["expression"]
        self.expressions.append(expression)
        if "setPref" in expression:
            self.pref, value = prefetch.PREFETCH_OFF, None
        elif "getPref" in expression:
            value = self.pref
        else:
            value = True
        if self.answers:
            reply = {"id": message["id"], "result": {"result": {"value": value}}}
            self.cdp.emit("Target.receivedMessageFromTarget", {"message": json.dumps(reply)})
        return {}


def browser_with(pref: object = 0, *, answers: bool = True) -> tuple[SimpleNamespace, FakeCDPSession, SettingsPage]:
    cdp = FakeCDPSession(
        {"Target.createTarget": {"targetId": "settings"}, "Target.attachToTarget": {"sessionId": "s1"}}
    )
    page = SettingsPage(cdp, pref=pref, answers=answers)
    cdp.replies["Target.sendMessageToTarget"] = page
    return SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=cdp)), cdp, page


def closed(cdp: FakeCDPSession) -> bool:
    return ("Target.closeTarget", {"targetId": "settings"}) in cdp.sent and cdp.detached


async def test_it_sets_the_pref_and_returns_what_it_reads_back() -> None:
    browser, cdp, page = browser_with(pref=0)

    assert await prefetch.turn_prefetch_off(browser) == prefetch.PREFETCH_OFF
    assert sum("setPref" in expression for expression in page.expressions) == 1
    assert ("Target.createTarget", {"url": prefetch.SETTINGS_URL}) in cdp.sent
    assert closed(cdp)


async def test_it_does_not_set_a_pref_that_is_already_off() -> None:
    browser, cdp, page = browser_with(pref=prefetch.PREFETCH_OFF)

    assert await prefetch.turn_prefetch_off(browser) == prefetch.PREFETCH_OFF
    assert not any("setPref" in expression for expression in page.expressions)
    assert closed(cdp)


async def test_a_settings_page_that_never_answers_is_bounded_and_reported() -> None:
    browser, cdp, _page = browser_with(answers=False)

    with patch.object(prefetch, "PREF_TIMEOUT_SECONDS", 0.05):
        result = await prefetch.turn_prefetch_off(browser)

    assert result == {"error": "TimeoutError: "}
    assert closed(cdp)


async def test_a_cdp_failure_is_reported_not_raised() -> None:
    browser, cdp, _page = browser_with()
    cdp.replies["Target.createTarget"] = RuntimeError("Target.createTarget: not allowed")

    result = await prefetch.turn_prefetch_off(browser)

    assert result == {"error": "RuntimeError: Target.createTarget: not allowed"}
    assert cdp.detached
