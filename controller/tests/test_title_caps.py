"""A page title is capped once, in Python, wherever the controller reads it (finding W4-18, W4-1).

PAGE_SUMMARY_SCRIPT capped `document.title`, but the session summary, the tab list, the fast perception preset and the
share snapshot read `page.title()` themselves and passed a megabyte title on to model prompts.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.browser_scripts import AB_TITLE_MAX
from app.providers.base import BaseProviderAdapter
from tests.test_allowed_hosts_rechecks import ALLOWED, MovingPage, manager_for

HUGE_TITLE = "T" * 1_000_000


def _session(manager, tmp_path: Path, *pages: MovingPage) -> SimpleNamespace:
    session = SimpleNamespace(
        id="s1",
        name="s1",
        created_at=datetime.now(UTC),
        page=pages[0],
        context=SimpleNamespace(pages=list(pages)),
        attached_pages=set(),
        console_messages=[],
        page_errors=[],
        request_failures=[],
        downloads=[],
        artifact_dir=tmp_path,
        lock=asyncio.Lock(),
        last_action=None,
        trace_path=tmp_path / "trace.zip",
        proxy_persona=None,
        protection_mode="normal",
        witness_remote_state=SimpleNamespace(model_dump=lambda: {}),
        owner=None,
    )
    manager.element_ids.evaluate = AsyncMock(return_value=[])
    manager.observation.page_summary = AsyncMock(
        return_value={
            "title": "active",
            "active_element": None,
            "text_excerpt": "",
            "dom_outline": {},
            "accessibility_outline": {},
        }
    )
    manager._current_takeover_url = lambda _session: None
    manager.remote_access = SimpleNamespace(session_info=lambda _session: {})
    manager.session_lifecycle.isolation_payload = lambda _session: {}
    manager.auth_profiles = SimpleNamespace(session_auth_state_info=lambda _session: None)
    return session


async def test_a_megabyte_title_is_capped_in_a_compact_observations_session_and_tab_list(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = _session(manager, tmp_path, MovingPage(ALLOWED, ALLOWED, title=HUGE_TITLE))

    observed = await manager.observation.observation_payload(session, preset="text")
    compact = BaseProviderAdapter.compact_observation(observed)

    assert len(compact["session"]["title"]) <= AB_TITLE_MAX
    assert compact["session"]["title"]
    assert [len(tab["title"]) for tab in compact["tabs"]] == [AB_TITLE_MAX]


async def test_the_fast_presets_title_is_capped(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = _session(manager, tmp_path, MovingPage(ALLOWED, ALLOWED, title=HUGE_TITLE))
    manager.observation._capture_screenshot_redacted = AsyncMock(
        return_value={"path": str(tmp_path / "fast.png"), "url": "/artifacts/s1/fast.png"}
    )

    observed = await manager.observation.observation_payload(session, preset="fast")

    assert len(observed["title"]) == AB_TITLE_MAX
