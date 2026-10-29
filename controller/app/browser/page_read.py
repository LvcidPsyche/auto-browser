"""Reads of a page that the agent receives, refused afterwards if the page moved while it was read.

Checking the page's URL before a read and again after it compares two instants. A page that
went to a refused host and came back, one the page guard sent to about:blank, and an active
page that closed and handed the session to another tab all passed both checks, and what was
read was the refused page's. A read captures the session's active page and that page's count
of refused commits (PageProvenance) before it reads. Afterwards it is refused if the active
page changed, the count changed, or the page is refused now (manager._page_refused).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

# How long a read waits for a session another call holds, and for the page to answer. A read
# holds the session lock while it reads, and a renderer stuck in a script never answers:
# unbounded, that stalled every action on the session and the page guard with it.
READ_LOCK_TIMEOUT_SECONDS = 10.0
PAGE_READ_TIMEOUT_SECONDS = 30.0

MOVED_DETAIL = (
    "The page moved while it was read: onto a host ALLOWED_HOSTS does not allow, or to another tab. "
    "What it showed is not returned."
)


class PageRead:
    """The session's active page and its count of refused commits, captured before a read."""

    def __init__(self, manager: Any, session: Any) -> None:
        self.manager = manager
        self.session = session
        self.page = session.page
        self.commits = manager.page_provenance.refused_commits(self.page)

    def moved(self) -> bool:
        """Whether what was read since may be a refused page's.

        A page that closed is refused too: closing forgets its provenance, count included,
        and the only tab stays the session's page, so its count reads 0 again.
        """
        return (
            self.session.page is not self.page
            or _closed(self.page)
            or self.manager.page_provenance.refused_commits(self.page) != self.commits
            or self.manager._page_refused(self.page)
        )

    def check(self, *files: str | Path | None) -> None:
        """Refuse what was read if the page moved meanwhile, and delete the files the read wrote."""
        if not self.moved():
            return
        _delete(files)
        raise PermissionError(MOVED_DETAIL)

    def refuse_raised(self, *files: str | Path | None) -> None:
        """In an except block: refuse in place of the read's error if the page moved meanwhile.

        A read that raises was checked by nobody, and a page can put its own text in the
        error (a script that throws it, a regex error that echoes it). The refusal is not
        chained to that error, so no log line or traceback carries it.
        """
        if not self.moved():
            return
        _delete(files)
        raise PermissionError(MOVED_DETAIL) from None


def _delete(files: tuple[str | Path | None, ...]) -> None:
    for file in files:
        if file:
            Path(file).unlink(missing_ok=True)


def _closed(page: Any) -> bool:
    is_closed = getattr(page, "is_closed", None)
    return callable(is_closed) and bool(is_closed())


def start_read(manager: Any, session: Any) -> PageRead:
    """Refuse a page ALLOWED_HOSTS refuses before it is read, and capture it for the check after."""
    manager._assert_page_allowed(session)
    return PageRead(manager, session)


@contextlib.asynccontextmanager
async def locked_read(manager: Any, session: Any) -> AsyncIterator[PageRead]:
    """A checked read for a path that does not hold the session lock already.

    Under the lock, the page guard and actions do not move the page while it is read. A
    refused page is turned away before the wait, and the wait is bounded.
    """
    manager._assert_page_allowed(session)
    try:
        await asyncio.wait_for(session.lock.acquire(), timeout=READ_LOCK_TIMEOUT_SECONDS)
    except TimeoutError:
        raise RuntimeError("The session is busy with another call. Try again.") from None
    try:
        read = start_read(manager, session)
        try:
            yield read
        except Exception:
            read.refuse_raised()
            raise
        read.check()
    finally:
        session.lock.release()


async def bounded(call: Awaitable[T], *, seconds: float | None = None) -> T:
    """A page call made under the session lock, bounded so a stuck renderer cannot keep it.

    ``seconds`` replaces PAGE_READ_TIMEOUT_SECONDS for a call that waits on the page by design (a drag).
    """
    limit = PAGE_READ_TIMEOUT_SECONDS if seconds is None else seconds
    try:
        return await asyncio.wait_for(call, timeout=limit)
    except TimeoutError:
        raise RuntimeError(f"The page did not answer within {limit:g} seconds.") from None


async def title_or_blank(manager: Any, session: Any) -> str:
    """The active page's title for a record (an approval, a saved profile), or "" if it is refused.

    "" also when the page moved while its title was read.
    """
    if manager._page_refused(session.page):
        return ""
    read = PageRead(manager, session)
    title = await bounded(session.page.title())
    return "" if read.moved() else title
