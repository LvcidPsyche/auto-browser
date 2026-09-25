from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ...navigation_policy import await_public_dns_check
from ..tab_view import TabView, set_tab_owner, tab_id_for, tab_owner, unwrap_session

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...browser_manager import BrowserSession


class BrowserTabService:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def list(self, session_id: str) -> list[dict[str, Any]]:
        session = unwrap_session(await self.manager.get_session(session_id))
        # Read-only: the shared side, so the owner's tab strip polling this
        # never waits behind (or holds up) the employees' tab actions.
        async with session.lock.shared():
            return await self.manager.session_lifecycle.guarded(
                session, self.summaries(session), what="list_tabs",
                timeout=self.manager.settings.browser_call_timeout_seconds,
            )

    async def open(
        self, session_id: str, url: str | None, activate: bool, *, owner: str | None = None
    ) -> dict[str, Any]:
        # Same host allowlist as navigate() and create_session(). This path
        # called page.goto() directly, so opening a tab reached any host —
        # internal services, cloud metadata — that ALLOWED_HOSTS refuses to
        # navigate to. Checked before a page exists, so a refusal leaves no tab.
        if url:
            self.manager._assert_url_allowed(url)
            await await_public_dns_check(self.manager, url)
        session = unwrap_session(await self.manager.get_session(session_id))
        timeout = self.manager.settings.browser_action_timeout_seconds
        # Only creating the tab needs the whole session. The first page load
        # (which can be slow) runs under the new tab's own lock, so it never
        # holds up the other employees' tabs.
        async with session.lock:
            new_page, tab_id = await self.manager.session_lifecycle.guarded(
                session, self._create_locked(session, activate, owner), what="open_tab", timeout=timeout,
            )
        if url:
            view = TabView(session, new_page, tab_id)
            async with view.lock:
                await self.manager.session_lifecycle.guarded(
                    view, self._load(view, url), what="open_tab", timeout=timeout,
                )
        async with session.lock.shared():
            pages = self.pages(session)
            new_index = pages.index(new_page) if new_page in pages else len(pages) - 1
            await self.manager._persist_session(session, status="active")
            return {
                "index": new_index,
                "activated": activate,
                "tab_id": tab_id,
                "owner": tab_owner(session, tab_id),
                "session": await self.manager._session_summary(session),
                "tabs": await self.summaries(session),
            }

    async def _create_locked(
        self, session: "BrowserSession", activate: bool, owner: str | None
    ) -> tuple["Page", str]:
        new_page = await session.context.new_page()
        self.manager._attach_page_listeners(new_page, session)
        tab_id = tab_id_for(session, new_page)
        set_tab_owner(session, tab_id, owner)
        if activate:
            session.page = new_page
            if hasattr(new_page, "bring_to_front"):
                await new_page.bring_to_front()
        return new_page, tab_id

    async def _load(self, view: TabView, url: str) -> None:
        await view.page.goto(url, wait_until="domcontentloaded")
        await self.manager._settle(view.page)

    async def activate(self, session_id: str, index: int) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            return await self.manager.session_lifecycle.guarded(
                session, self._activate_locked(session, index), what="activate_tab",
                timeout=self.manager.settings.browser_action_timeout_seconds,
            )

    async def _activate_locked(self, session: "BrowserSession", index: int) -> dict[str, Any]:
        pages = self.pages(session)
        if index < 0 or index >= len(pages):
            raise ValueError(f"Unknown tab index: {index}")
        target_page = pages[index]
        self.manager._attach_page_listeners(target_page, session)
        if hasattr(target_page, "bring_to_front"):
            await target_page.bring_to_front()
        session.page = target_page
        await self.manager._settle(session.page)
        await self.manager._persist_session(session, status="active")
        return {
            "index": index,
            "session": await self.manager._session_summary(session),
            "tabs": await self.summaries(session),
        }

    async def close(self, session_id: str, index: int) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            return await self.manager.session_lifecycle.guarded(
                session, self._close_locked(session, index), what="close_tab",
                timeout=self.manager.settings.browser_action_timeout_seconds,
            )

    async def _close_locked(self, session: "BrowserSession", index: int) -> dict[str, Any]:
        pages = self.pages(session)
        if index < 0 or index >= len(pages):
            raise ValueError(f"Unknown tab index: {index}")
        if len(pages) == 1:
            raise ValueError("Cannot close the only open tab in a session")
        target_page = pages[index]
        was_active = target_page is session.page
        closed_tab_id = tab_id_for(session, target_page)
        await target_page.close()
        set_tab_owner(session, closed_tab_id, None)
        remaining = self.pages(session)
        if was_active and remaining:
            session.page = remaining[max(0, min(index, len(remaining) - 1))]
            self.manager._attach_page_listeners(session.page, session)
            if hasattr(session.page, "bring_to_front"):
                await session.page.bring_to_front()
            await self.manager._settle(session.page)
        await self.manager._persist_session(session, status="active")
        return {
            "closed_index": index,
            "session": await self.manager._session_summary(session),
            "tabs": await self.summaries(session),
        }

    def pages(self, session: "BrowserSession") -> list["Page"]:
        session = unwrap_session(session)
        pages = getattr(session.context, "pages", None)
        if callable(pages):
            pages = pages()
        if isinstance(pages, list) and pages:
            return pages
        return [session.page]

    async def summaries(self, session: "BrowserSession") -> list[dict[str, Any]]:
        # "active" is the owner's active tab (the one in the live view), even
        # when asked through one employee's tab view.
        session = unwrap_session(session)
        tabs: list[dict[str, Any]] = []
        for index, page in enumerate(self.pages(session)):
            self.manager._attach_page_listeners(page, session)
            try:
                # One wedged background tab must not stall the whole list
                # (and the session lock behind it).
                title = await asyncio.wait_for(page.title(), 5)
            except Exception:
                title = ""
            tab_id = tab_id_for(session, page)
            tabs.append(
                {
                    "index": index,
                    "active": page is session.page,
                    "url": getattr(page, "url", ""),
                    "title": title,
                    "tab_id": tab_id,
                    "owner": tab_owner(session, tab_id),
                }
            )
        return tabs
