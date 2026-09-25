from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...browser_manager import BrowserSession


class BrowserTabService:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def list(self, session_id: str) -> list[dict[str, Any]]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            return await self.manager.session_lifecycle.guarded(
                session, self.summaries(session), what="list_tabs",
                timeout=self.manager.settings.browser_call_timeout_seconds,
            )

    async def open(self, session_id: str, url: str | None, activate: bool) -> dict[str, Any]:
        # Same host allowlist as navigate() and create_session(). This path
        # called page.goto() directly, so opening a tab reached any host —
        # internal services, cloud metadata — that ALLOWED_HOSTS refuses to
        # navigate to. Checked before a page exists, so a refusal leaves no tab.
        if url:
            self.manager._assert_url_allowed(url)
        session = await self.manager.get_session(session_id)
        async with session.lock:
            return await self.manager.session_lifecycle.guarded(
                session, self._open_locked(session, url, activate), what="open_tab",
                timeout=self.manager.settings.browser_action_timeout_seconds,
            )

    async def _open_locked(self, session: "BrowserSession", url: str | None, activate: bool) -> dict[str, Any]:
        new_page = await session.context.new_page()
        self.manager._attach_page_listeners(new_page, session)
        if url:
            await new_page.goto(url, wait_until="domcontentloaded")
            await self.manager._settle(new_page)
        if activate:
            session.page = new_page
            if hasattr(new_page, "bring_to_front"):
                await new_page.bring_to_front()
        pages = self.pages(session)
        new_index = pages.index(new_page) if new_page in pages else len(pages) - 1
        await self.manager._persist_session(session, status="active")
        return {
            "index": new_index,
            "activated": activate,
            "session": await self.manager._session_summary(session),
            "tabs": await self.summaries(session),
        }

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
        await target_page.close()
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
        pages = getattr(session.context, "pages", None)
        if callable(pages):
            pages = pages()
        if isinstance(pages, list) and pages:
            return pages
        return [session.page]

    async def summaries(self, session: "BrowserSession") -> list[dict[str, Any]]:
        tabs: list[dict[str, Any]] = []
        for index, page in enumerate(self.pages(session)):
            self.manager._attach_page_listeners(page, session)
            try:
                # One wedged background tab must not stall the whole list
                # (and the session lock behind it).
                title = await asyncio.wait_for(page.title(), 5)
            except Exception:
                title = ""
            tabs.append(
                {
                    "index": index,
                    "active": page is session.page,
                    "url": getattr(page, "url", ""),
                    "title": title,
                }
            )
        return tabs
