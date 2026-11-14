"""A navigation onto a bot challenge asks for one takeover, in Chromium, through the real BrowserManager and pipeline.

navigate probed its page for a challenge in its own operation and asked for a takeover; the pipeline probes the page
again after every action, asks for a takeover and fails the action with captcha_detected. So each challenge wrote two
takeovers, and one that passed between the probes left a takeover asked for under a navigation reported done. Only
the page server is local (test_allowed_hosts_navigation's harness); the challenge probe, the takeover service and the
records (actions.jsonl, the audit log) are the real ones. test_navigate_bot_challenge.py holds the same with fakes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.action_errors import BrowserActionError
from tests._chromium import requires_chromium
from tests.test_allowed_hosts_navigation import (  # noqa: F401 - _shared_chromium: that module's autouse browser
    _shared_chromium,
    audit_spy,
    browser_env,
)

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

ORDINARY = "<title>Example Domain</title><p>hello</p>"
# Cloudflare's interstitial, as the real probe reads it: by its title.
CHALLENGE = "<title>Just a moment...</title><p>Checking your browser before accessing the site.</p>"


def _count_probes(manager: Any) -> list[Any]:
    """Every answer of the real challenge probe, in order."""
    answers: list[Any] = []
    real = manager.bot_challenge.check

    async def counted(session: Any) -> Any:
        answer = await real(session)
        answers.append(answer)
        return answer

    manager.bot_challenge.check = counted
    return answers


def _takeover_lines(session: Any) -> list[dict[str, Any]]:
    path = Path(session.artifact_dir) / "actions.jsonl"
    if not path.exists():
        return []
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [line for line in lines if line.get("action") == "request_human_takeover"]


def _takeover_events(spy: Any) -> list[dict[str, Any]]:
    return [call.kwargs for call in spy.call_args_list if call.kwargs.get("event_type") == "takeover_requested"]


async def test_a_navigation_onto_a_challenge_asks_for_one_takeover(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": ORDINARY, "/challenge": CHALLENGE}) as env:
        session_id = await env.create("/")
        session = env.manager.sessions[session_id]
        spy = audit_spy(env)
        probes = _count_probes(env.manager)

        with pytest.raises(BrowserActionError) as failed:
            await env.manager.navigate(session_id, url=f"{env.allowed}/challenge")

        assert failed.value.code == "captcha_detected"
        assert (len(probes), len(_takeover_events(spy)), len(_takeover_lines(session))) == (1, 1, 1)


async def test_an_ordinary_navigation_probes_its_page_once(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": ORDINARY, "/next": ORDINARY}) as env:
        session_id = await env.create("/")
        probes = _count_probes(env.manager)

        result = await env.manager.navigate(session_id, url=f"{env.allowed}/next")

        assert result["action"] == "navigate"
        assert len(probes) == 1


async def test_a_navigation_reported_done_asked_for_no_takeover(tmp_path: Path) -> None:
    # The interstitial passes by itself after the navigation's settle and before the pipeline's probe: its title is
    # changed at the pipeline's TOTP step, between the two. The timing is the test's; the page, the probe and the
    # takeover are real.
    async with browser_env(tmp_path, {"/": ORDINARY, "/challenge": CHALLENGE}) as env:
        session_id = await env.create("/")
        session = env.manager.sessions[session_id]
        spy = audit_spy(env)
        real_totp = env.manager._maybe_handle_totp

        async def interstitial_passes(the_session: Any) -> Any:
            await the_session.page.evaluate("() => { document.title = 'Example Domain'; }")
            return await real_totp(the_session)

        env.manager._maybe_handle_totp = interstitial_passes

        result = await env.manager.navigate(session_id, url=f"{env.allowed}/challenge")

        assert result["action"] == "navigate"
        assert (len(_takeover_events(spy)), len(_takeover_lines(session))) == (0, 0)
