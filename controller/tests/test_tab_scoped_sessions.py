"""Per-employee tabs: stable tab ids + owners, a shared/exclusive session lock,
tab-scoped views (X-Tab-Id), and focus safety for the owner's live view."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
import weakref
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.action_errors import BrowserActionError
from app.browser.services.dialogs import BrowserDialogService
from app.browser.session_lock import SessionLock
from app.browser.tab_scope import current_tab_id, detached_context, is_valid_tab_id
from app.browser.tab_view import TabView, bind_tab, page_for_tab, tab_id_for, unwrap_session
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.middleware.tab_scope import TabScopeMiddleware, is_tab_scoped_path
from app.utils import UTC, spawn_background_task

# --- SessionLock ------------------------------------------------------------------------------


class SessionLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_exclusive_is_a_drop_in_for_asyncio_lock(self) -> None:
        lock = SessionLock()
        self.assertFalse(lock.locked())
        async with lock:
            self.assertTrue(lock.locked())
        self.assertFalse(lock.locked())
        with self.assertRaises(RuntimeError):
            lock.release()

    async def test_shared_holders_run_concurrently(self) -> None:
        lock = SessionLock()
        inside = 0
        peak = 0

        async def reader() -> None:
            nonlocal inside, peak
            async with lock.shared():
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.05)
                inside -= 1

        await asyncio.gather(*(reader() for _ in range(4)))
        self.assertEqual(peak, 4)
        self.assertFalse(lock.locked())

    async def test_exclusive_waits_for_all_shared_holders(self) -> None:
        lock = SessionLock()
        await lock.acquire_shared()
        await lock.acquire_shared()
        writer = asyncio.ensure_future(lock.acquire())
        await asyncio.sleep(0.01)
        self.assertFalse(writer.done())
        lock.release_shared()
        await asyncio.sleep(0.01)
        self.assertFalse(writer.done(), "one reader still holds it")
        lock.release_shared()
        await asyncio.sleep(0)
        self.assertTrue(writer.done())
        lock.release()
        self.assertFalse(lock.locked())

    async def test_new_readers_wait_behind_a_queued_writer(self) -> None:
        lock = SessionLock()
        order: list[str] = []
        await lock.acquire_shared()

        async def writer() -> None:
            async with lock:
                order.append("writer")
                await asyncio.sleep(0.01)

        async def late_reader() -> None:
            async with lock.shared():
                order.append("reader")

        writer_task = asyncio.ensure_future(writer())
        await asyncio.sleep(0)
        reader_task = asyncio.ensure_future(late_reader())
        await asyncio.sleep(0.01)
        self.assertEqual(order, [], "the late reader does not jump the queued writer")
        self.assertEqual(lock.exclusive_waiting, 1)
        lock.release_shared()
        await asyncio.gather(writer_task, reader_task)
        self.assertEqual(order, ["writer", "reader"])
        self.assertFalse(lock.locked())

    async def test_exclusive_blocks_shared_and_exclusive(self) -> None:
        lock = SessionLock()
        await lock.acquire()
        reader = asyncio.ensure_future(lock.acquire_shared())
        writer = asyncio.ensure_future(lock.acquire())
        await asyncio.sleep(0.01)
        self.assertFalse(reader.done() or writer.done())
        lock.release()
        await asyncio.sleep(0)
        self.assertTrue(reader.done())
        self.assertFalse(writer.done(), "arrival order: the reader first, then the writer")
        lock.release_shared()
        await asyncio.sleep(0)
        self.assertTrue(writer.done())
        lock.release()

    async def test_cancelled_writer_frees_the_readers_queued_behind_it(self) -> None:
        lock = SessionLock()
        await lock.acquire_shared()
        writer = asyncio.ensure_future(lock.acquire())
        await asyncio.sleep(0)
        reader = asyncio.ensure_future(lock.acquire_shared())
        await asyncio.sleep(0)
        self.assertFalse(reader.done())
        writer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await writer
        await asyncio.sleep(0)
        self.assertTrue(reader.done())
        self.assertEqual(lock.shared_holders, 2)
        self.assertEqual(lock.exclusive_waiting, 0)
        lock.release_shared()
        lock.release_shared()
        self.assertFalse(lock.locked())

    async def test_waiter_cancelled_after_being_granted_gives_it_back(self) -> None:
        lock = SessionLock()
        await lock.acquire()
        waiter = asyncio.ensure_future(lock.acquire())
        await asyncio.sleep(0)
        lock.release()  # grants `waiter` (its future is resolved) ...
        waiter.cancel()  # ... but it is cancelled before it resumes
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(lock.locked(), "the grant was handed back")
        async with lock:
            pass

    async def test_cancelled_shared_waiter_leaves_counters_exact(self) -> None:
        lock = SessionLock()
        await lock.acquire()
        reader = asyncio.ensure_future(lock.acquire_shared())
        await asyncio.sleep(0)
        reader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reader
        lock.release()
        self.assertFalse(lock.locked())
        self.assertEqual(lock.shared_holders, 0)


# --- fakes ------------------------------------------------------------------------------------


class FakePage:
    def __init__(self, context: "FakeContext", url: str, title: str = "") -> None:
        self.context = context
        self.url = url
        self._title = title
        self.closed = False
        self.front_calls = 0
        self.handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def is_closed(self) -> bool:
        return self.closed

    async def title(self) -> str:
        return self._title

    async def bring_to_front(self) -> None:
        self.front_calls += 1

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    async def close(self) -> None:
        self.closed = True
        if self in self.context.pages:
            self.context.pages.remove(self)


class FakeContext:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []

    async def new_page(self) -> FakePage:
        page = FakePage(self, "about:blank", "New Tab")
        self.pages.append(page)
        return page


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        ARTIFACT_ROOT=str(root / "artifacts"),
        AUTH_ROOT=str(root / "auth"),
        UPLOAD_ROOT=str(root / "uploads"),
        APPROVAL_ROOT=str(root / "approvals"),
        AUDIT_ROOT=str(root / "audit"),
        WITNESS_ROOT=str(root / "witness"),
        SESSION_STORE_ROOT=str(root / "sessions"),
    )


class _ManagerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.settings = _settings(root)
        self.manager = BrowserManager(self.settings)
        self.manager._persist_session = AsyncMock()  # type: ignore[method-assign]
        self.manager._settle = AsyncMock()  # type: ignore[method-assign]
        self.context = FakeContext()
        self.owner_page = FakePage(self.context, "https://example.com/", "Owner")
        self.context.pages.append(self.owner_page)
        artifact_dir = root / "artifacts" / "s1"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.session = BrowserSession(
            id="s1",
            name="s1",
            created_at=datetime.now(UTC),
            context=self.context,  # type: ignore[arg-type]
            page=self.owner_page,  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=root / "auth" / "s1",
            upload_dir=root / "uploads" / "s1",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        self.manager.sessions["s1"] = self.session

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def open(self, owner: str | None, *, activate: bool = False, url: str | None = None) -> dict:
        return await self.manager.open_tab("s1", url, activate, owner=owner)

    async def view(self, tab_id: str) -> TabView:
        token = current_tab_id.set(tab_id)
        try:
            return await self.manager.get_session("s1")  # type: ignore[return-value]
        finally:
            current_tab_id.reset(token)


# --- tabs: ids, owners, open_tab ---------------------------------------------------------------


class TabIdentityTests(_ManagerCase):
    async def test_open_tab_without_activate_keeps_the_owner_page_and_assigns_owner(self) -> None:
        result = await self.open("emad", activate=False)

        self.assertIs(self.session.page, self.owner_page, "the owner's active tab is unchanged")
        self.assertEqual(self.owner_page.front_calls, 0)
        new_page = self.context.pages[-1]
        self.assertEqual(new_page.front_calls, 0, "nothing is brought to the front")
        self.assertTrue(is_valid_tab_id(result["tab_id"]))
        self.assertEqual(result["owner"], "emad")
        self.assertEqual(result["activated"], False)
        self.assertEqual(result["index"], 1)
        for key in ("index", "activated", "session", "tabs", "tab_id", "owner"):
            self.assertIn(key, result)
        self.assertEqual(tab_id_for(self.session, new_page), result["tab_id"])

    async def test_open_tab_with_url_loads_it_in_the_new_tab_only(self) -> None:
        result = await self.open("ziad", url="https://example.com/next")
        new_page = self.context.pages[-1]
        self.assertEqual(new_page.url, "https://example.com/next")
        self.assertEqual(self.owner_page.url, "https://example.com/")
        self.assertEqual(result["tabs"][result["index"]]["url"], "https://example.com/next")
        self.assertFalse(self.session.lock.locked(), "every lock was released")

    async def test_summaries_carry_tab_id_and_owner(self) -> None:
        opened = await self.open("nihad")
        tabs = await self.manager.list_tabs("s1")
        self.assertEqual(len(tabs), 2)
        for tab in tabs:
            for key in ("index", "active", "url", "title", "tab_id", "owner"):
                self.assertIn(key, tab)
            self.assertTrue(is_valid_tab_id(tab["tab_id"]))
        self.assertEqual(tabs[0]["owner"], None)
        self.assertTrue(tabs[0]["active"])
        self.assertEqual(tabs[1]["owner"], "nihad")
        self.assertEqual(tabs[1]["tab_id"], opened["tab_id"])
        # Stable across calls.
        again = await self.manager.list_tabs("s1")
        self.assertEqual([t["tab_id"] for t in tabs], [t["tab_id"] for t in again])

    async def test_closing_a_tab_forgets_its_owner(self) -> None:
        opened = await self.open("emad")
        await self.manager.close_tab("s1", 1)
        self.assertNotIn(opened["tab_id"], self.session.tab_owners)


# --- TabView + get_session -------------------------------------------------------------------


class TabViewTests(_ManagerCase):
    async def test_get_session_resolves_the_tab_and_410s_when_gone(self) -> None:
        opened = await self.open("emad")
        view = await self.view(opened["tab_id"])
        self.assertIsInstance(view, TabView)
        self.assertTrue(view.tab_scoped)
        self.assertIs(view.page, self.context.pages[-1])
        self.assertIs(unwrap_session(view), self.session)
        # Without a scope: the real session, exactly as before.
        self.assertIs(await self.manager.get_session("s1"), self.session)

        await self.context.pages[-1].close()
        with self.assertRaises(BrowserActionError) as caught:
            await self.view(opened["tab_id"])
        self.assertEqual(caught.exception.code, "tab_gone")
        self.assertEqual(caught.exception.status_code, 410)
        self.assertTrue(caught.exception.retryable)
        with self.assertRaises(BrowserActionError):
            await self.view("t-000000000000")

    async def test_view_delegates_reads_and_writes_except_page(self) -> None:
        opened = await self.open("emad")
        view = await self.view(opened["tab_id"])
        self.assertEqual(view.id, "s1")
        view.last_action = "click"
        self.assertEqual(self.session.last_action, "click")
        view.agent_action_depth += 1
        self.assertEqual(self.session.agent_action_depth, 1)
        view.mouse_position = (3.0, 4.0)
        self.assertIsNone(self.session.mouse_position, "mouse position is kept per tab")
        self.assertEqual(view.mouse_position, (3.0, 4.0))
        with self.assertRaises(AttributeError):
            view.tab_id = "t-111111111111"

    async def test_rebinding_the_view_page_keeps_the_tab_id_and_the_owner_focus(self) -> None:
        opened = await self.open("emad")
        tab_id = opened["tab_id"]
        view = await self.view(tab_id)
        opener = view.page
        popup = FakePage(self.context, "https://accounts.example.com/", "Sign in")
        self.context.pages.append(popup)

        view.page = popup

        self.assertIs(view.page, popup)
        self.assertIs(self.session.page, self.owner_page, "the owner's active tab never moves")
        self.assertEqual(tab_id_for(self.session, popup), tab_id)
        self.assertNotEqual(tab_id_for(self.session, opener), tab_id)
        tabs = await self.manager.list_tabs("s1")
        owners = {tab["tab_id"]: tab["owner"] for tab in tabs}
        self.assertEqual(owners[tab_id], "emad")
        self.assertEqual(owners[tab_id_for(self.session, opener)], "emad", "the opener stays his too")
        # The next scoped call lands on the popup.
        self.assertIs((await self.view(tab_id)).page, popup)

    async def test_a_closed_popup_falls_back_to_its_opener_for_that_tab(self) -> None:
        opened = await self.open("emad")
        tab_id = opened["tab_id"]
        view = await self.view(tab_id)
        opener = view.page
        popup = FakePage(self.context, "https://accounts.example.com/", "Sign in")
        self.context.pages.append(popup)
        self.session.popup_openers[popup] = opener
        view.page = popup
        await popup.close()

        again = await self.view(tab_id)
        self.assertIs(again.page, opener)
        self.assertEqual(tab_id_for(self.session, opener), tab_id)

    async def test_two_views_on_different_pages_run_concurrently(self) -> None:
        first = await self.open("emad")
        second = await self.open("ziad")
        view_a = await self.view(first["tab_id"])
        view_b = await self.view(second["tab_id"])
        inside = 0
        peak = 0

        async def work(view: TabView) -> None:
            nonlocal inside, peak
            async with view.lock:
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.05)
                inside -= 1

        await asyncio.gather(work(view_a), work(view_b))
        self.assertEqual(peak, 2)
        self.assertFalse(self.session.lock.locked())

    async def test_same_page_serialises(self) -> None:
        opened = await self.open("emad")
        view_a = await self.view(opened["tab_id"])
        view_b = await self.view(opened["tab_id"])
        inside = 0
        peak = 0

        async def work(view: TabView) -> None:
            nonlocal inside, peak
            async with view.lock:
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.02)
                inside -= 1

        await asyncio.gather(work(view_a), work(view_b), work(view_a))
        self.assertEqual(peak, 1)

    async def test_exclusive_waits_for_views_and_blocks_new_ones(self) -> None:
        first = await self.open("emad")
        second = await self.open("ziad")
        view_a = await self.view(first["tab_id"])
        view_b = await self.view(second["tab_id"])
        events: list[str] = []
        release_a = asyncio.Event()

        async def hold_a() -> None:
            async with view_a.lock:
                events.append("a-in")
                await release_a.wait()
                events.append("a-out")

        async def exclusive() -> None:
            async with self.session.lock:
                events.append("exclusive")

        async def late_b() -> None:
            async with view_b.lock:
                events.append("b-in")

        task_a = asyncio.ensure_future(hold_a())
        await asyncio.sleep(0.01)
        task_x = asyncio.ensure_future(exclusive())
        await asyncio.sleep(0.01)
        task_b = asyncio.ensure_future(late_b())
        await asyncio.sleep(0.01)
        self.assertEqual(events, ["a-in"], "exclusive waits for A; B waits behind the exclusive")
        release_a.set()
        await asyncio.gather(task_a, task_x, task_b)
        self.assertEqual(events, ["a-in", "a-out", "exclusive", "b-in"])

    async def test_cancelled_view_waiter_releases_its_shared_hold(self) -> None:
        opened = await self.open("emad")
        view = await self.view(opened["tab_id"])
        holder_in = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with view.lock:
                holder_in.set()
                await release.wait()

        async def waiter() -> None:
            async with view.lock:
                pass

        hold = asyncio.ensure_future(holder())
        await holder_in.wait()
        wait = asyncio.ensure_future(waiter())
        await asyncio.sleep(0.01)
        self.assertEqual(self.session.lock.shared_holders, 2, "the waiter holds shared while it waits for the tab")
        wait.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await wait
        self.assertEqual(self.session.lock.shared_holders, 1)
        release.set()
        await hold
        self.assertFalse(self.session.lock.locked())

    async def test_page_lock_follows_the_tab_through_a_rebind(self) -> None:
        opened = await self.open("emad")
        view = await self.view(opened["tab_id"])
        popup = FakePage(self.context, "https://accounts.example.com/", "Sign in")
        self.context.pages.append(popup)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with view.lock:
                view.page = popup  # popup follow in the middle of an action
                entered.set()
                await release.wait()

        task = asyncio.ensure_future(holder())
        await entered.wait()
        # A second call on the same tab id now resolves to the popup, and must
        # still queue behind the in-flight action on that tab.
        again = await self.view(opened["tab_id"])
        self.assertIs(again.page, popup)
        second_lock = again.lock
        second = asyncio.ensure_future(second_lock.__aenter__())
        await asyncio.sleep(0.01)
        self.assertFalse(second.done())
        release.set()
        await task
        await second
        await second_lock.__aexit__(None, None, None)
        self.assertFalse(self.session.lock.locked())


# --- actions through a view ------------------------------------------------------------------


class TabScopedActionTests(_ManagerCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.manager.observation.observation_payload = AsyncMock(  # type: ignore[method-assign]
            return_value={"url": "x", "title": "t"}
        )
        self.manager.observation.light_snapshot = AsyncMock(  # type: ignore[method-assign]
            return_value={"url": "x"}
        )
        self.manager._record_witness_receipt = AsyncMock()  # type: ignore[method-assign]
        self.manager._check_bot_challenge = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self.manager._maybe_handle_totp = AsyncMock(return_value=None)  # type: ignore[method-assign]

    async def test_wait_actions_on_two_tabs_overlap_and_the_owner_page_is_untouched(self) -> None:
        first = await self.open("emad")
        second = await self.open("ziad")

        async def scoped_wait(tab_id: str) -> float:
            token = current_tab_id.set(tab_id)
            try:
                started = asyncio.get_running_loop().time()
                await self.manager.wait("s1", 300)
                return asyncio.get_running_loop().time() - started
            finally:
                current_tab_id.reset(token)

        loop = asyncio.get_running_loop()
        began = loop.time()
        await asyncio.gather(scoped_wait(first["tab_id"]), scoped_wait(second["tab_id"]))
        elapsed = loop.time() - began
        self.assertLess(elapsed, 0.55, f"ran side by side, took {elapsed:.2f}s")
        self.assertIs(self.session.page, self.owner_page)
        self.assertFalse(self.session.lock.locked())

    async def test_popup_through_a_view_never_steals_focus(self) -> None:
        opened = await self.open("emad")
        view = await self.view(opened["tab_id"])
        dialogs: BrowserDialogService = self.manager.dialogs
        popup = FakePage(self.context, "https://accounts.example.com/", "Sign in")
        self.context.pages.append(popup)
        self.session.popup_openers[popup] = view.page
        self.session.pending_popup = popup

        followed = await dialogs.follow_popup(view)  # type: ignore[arg-type]

        self.assertEqual(followed, {"followed_popup": True, "url": "https://accounts.example.com/"})
        self.assertEqual(popup.front_calls, 0, "no bring_to_front for a tab-scoped action")
        self.assertIs(self.session.page, self.owner_page)
        self.assertIs(view.page, popup)

    async def test_a_view_does_not_follow_another_tabs_popup(self) -> None:
        first = await self.open("emad")
        second = await self.open("ziad")
        view_a = await self.view(first["tab_id"])
        view_b = await self.view(second["tab_id"])
        popup = FakePage(self.context, "https://accounts.example.com/", "Sign in")
        self.context.pages.append(popup)
        self.session.popup_openers[popup] = view_a.page
        self.session.pending_popup = popup

        self.assertIsNone(await self.manager.dialogs.follow_popup(view_b))  # type: ignore[arg-type]
        self.assertIs(self.session.pending_popup, popup, "left for tab A's action")
        self.assertIsNotNone(await self.manager.dialogs.follow_popup(view_a))  # type: ignore[arg-type]
        self.assertIs(view_a.page, popup)

    async def test_heal_through_a_view_never_jumps_into_another_tab(self) -> None:
        first = await self.open("emad")
        view = await self.view(first["tab_id"])
        await view.page.close()
        self.assertFalse(self.manager.dialogs.heal_active_page(view))  # type: ignore[arg-type]
        self.assertIs(self.session.page, self.owner_page)

    async def test_listeners_attached_through_a_view_bind_to_the_real_session(self) -> None:
        opened = await self.open("emad")
        view = await self.view(opened["tab_id"])
        page = FakePage(self.context, "https://x/", "x")
        self.manager._attach_page_listeners(page, view)  # type: ignore[arg-type]
        self.assertIn(page, self.session.attached_pages)
        page.handlers["pageerror"][0]("boom")
        self.assertEqual(self.session.page_errors[-1], "boom")


class TabScopedRouteTests(_ManagerCase):
    """Through the real sessions router: X-Tab-Id reaches get_session, and the
    controller's own error codes (410 tab_gone) come back with their status."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        import httpx
        from fastapi.responses import JSONResponse

        from app.routes.sessions import create_sessions_router

        app = FastAPI()
        app.add_middleware(TabScopeMiddleware)
        app.include_router(create_sessions_router(manager=self.manager))

        @app.exception_handler(BrowserActionError)
        async def handle(_request, exc: BrowserActionError) -> JSONResponse:
            return JSONResponse(status_code=exc.status_code, content=exc.payload)

        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://controller")

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        await super().asyncTearDown()

    async def test_gone_tab_answers_410_on_every_scoped_route(self) -> None:
        header = {"X-Tab-Id": "t-000000000000"}
        requests = [
            ("GET", "/sessions/s1/observe", None),
            ("POST", "/sessions/s1/observe", {}),
            ("POST", "/sessions/s1/screenshot", {}),
            ("POST", "/sessions/s1/actions/wait", {"wait_ms": 10}),
            ("POST", "/sessions/s1/actions/dialog", {"accept": True}),
            ("POST", "/sessions/s1/actions/click", {"x": 1, "y": 1}),
            ("POST", "/sessions/s1/actions/navigate", {"url": "https://example.com/"}),
            ("POST", "/sessions/s1/actions/reload", None),
        ]
        for method, path, body in requests:
            response = await self.http.request(method, path, headers=header, json=body)
            self.assertEqual(response.status_code, 410, path)
            self.assertEqual(response.json()["code"], "tab_gone", path)

    async def test_open_tab_route_accepts_an_owner_label(self) -> None:
        response = await self.http.post("/sessions/s1/tabs/open", json={"activate": False, "owner": "emad"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["owner"], "emad")
        bad = await self.http.post("/sessions/s1/tabs/open", json={"activate": False, "owner": "Emad Ali"})
        self.assertEqual(bad.status_code, 422)


# --- request scoping -------------------------------------------------------------------------


class TabScopeMiddlewareTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.add_middleware(TabScopeMiddleware)

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def echo(path: str) -> dict:
            return {"tab": current_tab_id.get()}

        self.client = TestClient(app)

    def test_scope_is_set_only_on_page_scoped_paths(self) -> None:
        header = {"X-Tab-Id": "t-0123456789ab"}
        scoped = [
            ("GET", "/sessions/s1/observe"),
            ("POST", "/sessions/s1/observe"),
            ("POST", "/sessions/s1/screenshot"),
            *[("POST", f"/sessions/s1/actions/{op}") for op in (
                "navigate", "click", "type", "press", "dialog", "scroll", "upload", "hover",
                "select-option", "wait", "reload", "go-back", "go-forward",
            )],
        ]
        for method, path in scoped:
            response = self.client.request(method, path, headers=header)
            self.assertEqual(response.json(), {"tab": "t-0123456789ab"}, path)
        unscoped = [
            ("GET", "/sessions/s1/tabs"),
            ("POST", "/sessions/s1/tabs/open"),
            ("POST", "/sessions/s1/tabs/activate"),
            ("POST", "/sessions/s1/actions/type-focused"),
            ("POST", "/sessions/s1/actions/execute"),
            ("GET", "/sessions/s1"),
            ("GET", "/sessions/s1/screenshot"),
            ("POST", "/sessions/s1/auth-profiles"),
        ]
        for method, path in unscoped:
            response = self.client.request(method, path, headers=header)
            self.assertEqual(response.json(), {"tab": None}, path)

    def test_invalid_tab_id_is_refused_on_scoped_paths_only(self) -> None:
        for bad in ("t-XYZ", "t-0123456789abc", "abc", "t-0123456789AB"):
            response = self.client.post("/sessions/s1/actions/click", headers={"X-Tab-Id": bad})
            self.assertEqual(response.status_code, 400, bad)
            self.assertEqual(response.json()["code"], "invalid_tab_id")
        self.assertEqual(
            self.client.get("/sessions/s1/tabs", headers={"X-Tab-Id": "bad"}).json(), {"tab": None}
        )

    def test_no_header_means_no_scope(self) -> None:
        self.assertEqual(self.client.post("/sessions/s1/actions/click").json(), {"tab": None})

    def test_path_matcher(self) -> None:
        self.assertTrue(is_tab_scoped_path("post", "/sessions/abc/actions/go-back"))
        self.assertFalse(is_tab_scoped_path("GET", "/sessions/abc/actions/click"))
        self.assertFalse(is_tab_scoped_path("POST", "/sessions/abc/actions/click/extra"))


class BackgroundTaskScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_tasks_never_inherit_the_tab_scope(self) -> None:
        seen: list = []

        async def probe() -> None:
            seen.append(current_tab_id.get())

        token = current_tab_id.set("t-0123456789ab")
        try:
            await spawn_background_task(probe())
            await asyncio.get_running_loop().create_task(probe(), context=detached_context())
            await asyncio.ensure_future(probe())  # a plain task would inherit it
        finally:
            current_tab_id.reset(token)
        self.assertEqual(seen, [None, None, "t-0123456789ab"])


class RegistryHelperTests(unittest.TestCase):
    def test_helpers_work_on_minimal_session_doubles(self) -> None:
        class P:
            def is_closed(self) -> bool:
                return False

        page_a, page_b = P(), P()
        session = SimpleNamespace(popup_openers=weakref.WeakKeyDictionary())
        tab = tab_id_for(session, page_a)
        self.assertTrue(is_valid_tab_id(tab))
        self.assertIs(page_for_tab(session, tab, [page_a, page_b]), page_a)
        bind_tab(session, page_b, tab)
        self.assertIs(page_for_tab(session, tab, [page_a, page_b]), page_b)
        self.assertNotEqual(tab_id_for(session, page_a), tab)


if __name__ == "__main__":
    unittest.main()
