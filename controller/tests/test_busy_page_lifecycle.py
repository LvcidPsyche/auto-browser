"""A page that never answers cannot take the session list or close_session down with it.

`page.title()` and `page.evaluate()` have no timeout, and a page running a
script in an endless loop never answers either. list_sessions summarised every
session through page.title(), so one such page hung the listing for everyone;
close_session summarised before releasing anything, and held the session lock
while it waited — and an action stuck on that page held the same lock first.
With MAX_SESSIONS=1 (the default) one visited page could wedge the controller
until a restart. Separately, listing iterated the live session dict across
awaits, so a session closing mid-list raised "dictionary changed size".
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.browser.services import sessions as sessions_module
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.utils import UTC
from tests._chromium import chromium_executable, requires_chromium


class SilentPage:
    """A page whose renderer never answers, like one running `while (true) {}`."""

    url = "https://busy.example/"

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        await asyncio.Event().wait()
        return ""


class StuckContext:
    """Closing it fails whatever is waiting on its page, as Playwright does."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def close(self) -> None:
        self.closed.set()

    async def wait_on_page(self) -> None:
        await self.closed.wait()
        raise RuntimeError("Target page, context or browser has been closed")


def _manager(root: Path) -> BrowserManager:
    settings = Settings(_env_file=None)
    for attr in (
        "artifact_root",
        "upload_root",
        "auth_root",
        "approval_root",
        "session_store_root",
        "audit_root",
        "witness_root",
    ):
        setattr(settings, attr, str(root / attr))
    return BrowserManager(settings)


def _session(manager: BrowserManager, session_id: str, *, page, context) -> BrowserSession:
    artifact_dir = Path(manager.settings.artifact_root) / session_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    session = BrowserSession(
        id=session_id,
        name=session_id,
        created_at=datetime.now(UTC),
        context=context,
        page=page,
        artifact_dir=artifact_dir,
        auth_dir=Path(manager.settings.auth_root) / session_id,
        upload_dir=Path(manager.settings.upload_root) / session_id,
        takeover_url="http://127.0.0.1:6080/vnc.html",
        trace_path=artifact_dir / "trace.zip",
    )
    manager.sessions[session_id] = session
    return session


class BusyPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = _manager(Path(self.tempdir.name))
        patcher = patch.multiple(sessions_module, PAGE_TITLE_TIMEOUT_SECONDS=0.05, CLOSE_LOCK_WAIT_SECONDS=0.2)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_listing_answers_although_a_page_does_not(self) -> None:
        _session(self.manager, "busy", page=SilentPage(), context=StuckContext())
        listed = await asyncio.wait_for(self.manager.list_sessions(), timeout=5)
        self.assertEqual([item["id"] for item in listed], ["busy"])

    async def test_close_gets_past_an_action_stuck_on_the_page(self) -> None:
        context = StuckContext()
        session = _session(self.manager, "busy", page=SilentPage(), context=context)

        async def stuck_action() -> None:
            async with session.lock:
                await context.wait_on_page()

        action = asyncio.create_task(stuck_action())
        await asyncio.sleep(0.01)
        result = await asyncio.wait_for(self.manager.close_session("busy"), timeout=5)

        self.assertTrue(result["closed"])
        self.assertNotIn("busy", self.manager.sessions)
        self.assertTrue(context.closed.is_set())
        with self.assertRaises(RuntimeError):
            await action

    async def test_a_session_closing_mid_list_does_not_break_the_listing(self) -> None:
        class SlowPage(SilentPage):
            async def title(self) -> str:
                await asyncio.sleep(0.01)
                return "slow"

        for index in range(3):
            _session(self.manager, f"s{index}", page=SlowPage(), context=StuckContext())

        async def close_one() -> None:
            await asyncio.sleep(0.005)
            self.manager.sessions.pop("s2", None)

        listed, _ = await asyncio.gather(self.manager.list_sessions(), close_one())
        self.assertTrue({"s0", "s1"} <= {item["id"] for item in listed})

    async def test_a_hung_teardown_step_does_not_hang_close(self) -> None:
        context = StuckContext()
        never_returns = asyncio.Event()

        async def hung_close() -> None:
            await never_returns.wait()

        context.close = hung_close
        _session(self.manager, "busy", page=SilentPage(), context=context)
        with patch.object(sessions_module, "TEARDOWN_STEP_TIMEOUT_SECONDS", 0.05):
            result = await asyncio.wait_for(self.manager.close_session("busy"), timeout=5)
        self.assertTrue(result["closed"])


@requires_chromium
def test_a_real_page_in_an_endless_loop_can_be_listed_and_closed(tmp_path) -> None:
    from playwright.async_api import async_playwright

    async def main() -> tuple[float, float]:
        manager = _manager(tmp_path)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable())
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.route(
                    "https://busy.example/**",
                    lambda route: route.fulfill(
                        body="<title>busy</title><script>setTimeout(() => { while (true) {} }, 100)</script>",
                        content_type="text/html",
                    ),
                )
                await page.goto("https://busy.example/")
                await asyncio.sleep(0.4)
                _session(manager, "busy", page=page, context=context)
                started = time.perf_counter()
                await asyncio.wait_for(manager.list_sessions(), timeout=20)
                listed_after = time.perf_counter() - started
                started = time.perf_counter()
                await asyncio.wait_for(manager.close_session("busy"), timeout=30)
                return listed_after, time.perf_counter() - started
            finally:
                await browser.close()

    listed_after, closed_after = asyncio.run(main())
    assert listed_after < 15 and closed_after < 25
