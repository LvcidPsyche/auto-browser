from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from playwright.async_api import Error as PlaywrightError

from ...action_errors import BrowserActionError
from ..page_provenance import CLOSED_PAGE_GRACE_SECONDS
from ..request_gate import block_summary, navigation_blocked_error, stopped_by_gate
from ..request_gates import CHROME_ERROR_SCHEME

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...browser_manager import BrowserSession


logger = logging.getLogger(__name__)

# How long the tab list waits for one tab's title before listing it without one.
TAB_TITLE_TIMEOUT_SECONDS = 2.0
# Why a tab is not activated (REST answers it too). Only the active page can be navigated, so the way out
# of a refused tab that is not active is to close it; it names no host.
REFUSED_TAB_MESSAGE = (
    "That tab is on a host ALLOWED_HOSTS does not allow, or came from one, and cannot be activated. Close it, "
    "or open a new tab."
)
# A tab refused only because another tab of the session was refused when it loaded, or had just closed
# (page_provenance's grace): its own page is allowed. Closing the refused tab does not clear it (only the
# agent's navigate does, and a tab that is not active cannot be navigated), so the advice is REFUSED_TAB_MESSAGE's.
REFUSED_BY_ANOTHER_TAB_MESSAGE = (
    "That tab loaded while another tab of this session was refused, or within "
    f"{CLOSED_PAGE_GRACE_SECONDS:g} seconds after a refused tab closed, which can send it to a URL it chose, "
    "and cannot be activated. Close it, or open a new tab."
)


class TabRefusedByAnotherTabError(PermissionError):
    """Activating a tab refused only because another tab of its session was; the text names no host."""


class BrowserTabService:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def list(self, session_id: str) -> list[dict[str, Any]]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            return await self.summaries(session)

    async def open(self, session_id: str, url: str | None, activate: bool) -> dict[str, Any]:
        # Same host allowlist as navigate() and create_session(). This path
        # called page.goto() directly, so opening a tab reached any host —
        # internal services, cloud metadata — that ALLOWED_HOSTS refuses to
        # navigate to. Checked before a page exists, so a refusal leaves no tab.
        if url:
            self.manager._assert_url_allowed(url)
        session = await self.manager.get_session(session_id)
        async with session.lock:
            if url:
                # As every action does (the pipeline): what the new page commits while it loads, right
                # after a refused tab closed, would count as that tab's choice.
                await self.manager._wait_out_closed_page_grace(session)
            gates = self.manager.request_gates
            # Before the tab exists: on a lost gate its navigation would go out unjudged.
            gates.assert_live(session)
            new_page = await session.context.new_page()
            # The browser's request sequence before the tab navigates: a block after it on this page is its own.
            blocked_before = gates.mark(session.browser)
            # Reported by the context like a popup, but with no opener to judge it.
            self.manager.page_provenance.opened_by_controller(new_page)
            if session.gate_context_id is not None:
                await gates.bypass_service_workers(new_page)
            try:
                self.manager._attach_page_listeners(new_page, session)
                if url:
                    # Again right before the navigation: the gate can be lost while the bypass is set.
                    gates.assert_live(session)
                    with self.manager.page_provenance.controller_navigation(new_page) as navigation:
                        navigation.response = await new_page.goto(url, wait_until="domcontentloaded")
                    await self.manager._settle(new_page)
                    # As _goto_or_blocked: the page loaded, then left for a refused host (a meta refresh, a
                    # script) while it settled, and the gate stopped it. Raised here, so the tab is closed below.
                    stopped = await gates.blocks_on_page(session.browser, new_page, since=blocked_before)
                    if stopped:
                        raise navigation_blocked_error(stopped[-1], action="open_tab")
                    # The check navigate() makes after the page loads: an
                    # allowlisted URL can redirect somewhere that is not.
                    self.manager._assert_runtime_url_allowed(new_page.url)
            except BaseException as exc:
                # Read while the tab is open: a block names its page by the page's targetId.
                stopped_here = isinstance(exc, PlaywrightError) and stopped_by_gate(exc)
                stopped = (
                    await gates.blocks_on_page(session.browser, new_page, since=blocked_before) if stopped_here else []
                )
                # A failed open (DNS error, timeout, refused redirect) left the
                # blank tab behind, unreported, and answered with a raw 500.
                try:
                    await new_page.close()
                except Exception as close_exc:
                    logger.debug("could not close the failed new tab: %s", close_exc)
                if isinstance(exc, PlaywrightError):
                    if stopped_here:
                        raise navigation_blocked_error(stopped[-1] if stopped else None, action="open_tab") from exc
                    raise BrowserActionError(
                        "Could not open the tab. Check the URL and retry.",
                        code="browser_action_failed",
                        action="open_tab",
                        retryable=True,
                        url=url,
                    ) from exc
                raise
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
            pages = self.pages(session)
            if index < 0 or index >= len(pages):
                raise ValueError(f"Unknown tab index: {index}")
            target_page = pages[index]
            # A popup or tab the page opened may sit on a host the allowlist
            # refuses, and every later action and observation reads session.page.
            # Refused before it becomes the active page, so the session stays
            # where it was.
            try:
                self.manager._assert_page_object_allowed(target_page)
            except PermissionError as exc:
                if self.manager.page_provenance.refused_by_another_page(target_page):
                    raise TabRefusedByAnotherTabError(REFUSED_BY_ANOTHER_TAB_MESSAGE) from exc
                raise PermissionError(REFUSED_TAB_MESSAGE) from exc
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
        pages = self.pages(session)
        for page in pages:
            self.manager._attach_page_listeners(page, session)
        # A tab ALLOWED_HOSTS refuses is listed by URL only: checked before the title
        # is read and after, by its count of refused commits too, since the page can
        # move while it is read and come back (see app/browser/page_read.py).
        provenance = self.manager.page_provenance
        before = [(self.manager._page_refused(page), provenance.refused_commits(page)) for page in pages]
        # Every observation lists the tabs; fetch the titles together rather
        # than paying one browser round trip per open tab in turn. A tab refused
        # already is not asked.
        titles = await asyncio.gather(*(self._title(page, refused=off) for page, (off, _) in zip(pages, before)))
        titles = [
            ""
            if off or self.manager._page_refused(page) or provenance.refused_commits(page) != commits or _closed(page)
            else title
            for page, title, (off, commits) in zip(pages, titles, before)
        ]
        tabs = [
            {
                "index": index,
                "active": page is session.page,
                "url": self.manager._shown_url(page),
                "title": title,
            }
            for index, (page, title) in enumerate(zip(pages, titles))
        ]
        gates = getattr(self.manager, "request_gates", None)
        for tab, page in zip(tabs, pages):
            # A tab listed on Chrome's error page: say what the pre-request gate stopped, while that block is the
            # page's last main-frame request. Decided on the URL listed, not the page's live one, so the two
            # agree; and so the gates are asked about error-page tabs only.
            if gates is None or not str(tab["url"]).startswith(CHROME_ERROR_SCHEME):
                continue
            block = await gates.last_block_on_page(getattr(session, "browser", None), page)
            if block is not None:
                tab["blocked_navigation"] = block_summary(block)
        return tabs

    @staticmethod
    async def _title(page: "Page", *, refused: bool) -> str:
        if refused:
            return ""
        try:
            # Bounded: the list is read under the session lock, and a tab stuck in a script never answers.
            return await asyncio.wait_for(page.title(), timeout=TAB_TITLE_TIMEOUT_SECONDS)
        except Exception:
            return ""


def _closed(page: Any) -> bool:
    """A tab that closed while its title was read: its provenance is gone with it."""
    is_closed = getattr(page, "is_closed", None)
    return callable(is_closed) and bool(is_closed())
