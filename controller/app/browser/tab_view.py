"""Tabs as first-class, lockable units of one browser session.

Every page gets a stable id (``t-`` + 12 hex chars) the first time it is
seen, and optionally an owner label (the employee who opened it). A request
carrying ``X-Tab-Id`` works on a ``TabView``: the real session seen through
that one tab.

Locking (see ``SessionLock``):

* ``view.lock`` = the session lock's SHARED side, then that tab's own page
  lock. Two employees in two tabs run side by side; two calls on the same tab
  queue; anything session-wide (tab open/activate/close, session close,
  re-attach, auth-profile save, calls without ``X-Tab-Id``) takes the
  EXCLUSIVE side, so it waits for in-flight tab calls and blocks new ones.
* The view never moves the owner's focus: ``view.page = popup`` re-binds the
  same tab id to the popup, but ``session.page`` (the owner's active tab, the
  one shown in the live view) is left alone.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any, Iterable

from .tab_scope import new_tab_id


def _registry(session: Any, name: str) -> Any:
    registry = getattr(session, name, None)
    if registry is None:
        registry = {} if name == "tab_owners" else weakref.WeakKeyDictionary()
        setattr(session, name, registry)
    return registry


def unwrap_session(session: Any) -> Any:
    """The real session behind a TabView (or the session itself)."""
    return session.real_session if getattr(session, "tab_scoped", False) is True else session


def tab_id_for(session: Any, page: Any) -> str:
    """The page's stable tab id, assigned on first sight."""
    session = unwrap_session(session)
    tab_ids = _registry(session, "tab_ids")
    tab_id = tab_ids.get(page)
    if tab_id is None:
        tab_id = new_tab_id()
        tab_ids[page] = tab_id
    return tab_id


def tab_owner(session: Any, tab_id: str) -> str | None:
    return _registry(unwrap_session(session), "tab_owners").get(tab_id)


def set_tab_owner(session: Any, tab_id: str, owner: str | None) -> None:
    owners = _registry(unwrap_session(session), "tab_owners")
    if owner:
        owners[tab_id] = owner
    else:
        owners.pop(tab_id, None)


def page_lock_for(session: Any, page: Any) -> asyncio.Lock:
    locks = _registry(unwrap_session(session), "page_locks")
    lock = locks.get(page)
    if lock is None:
        lock = asyncio.Lock()
        locks[page] = lock
    return lock


def bind_tab(session: Any, page: Any, tab_id: str) -> None:
    """Point ``tab_id`` at ``page`` (popup follow / return to opener).

    The tab keeps its id, owner and lock. A still-open page that held the id
    before (the opener of a followed popup) gets a fresh id with the same
    owner, so the employee's other window stays labelled as his.
    """
    session = unwrap_session(session)
    tab_ids = _registry(session, "tab_ids")
    owners = _registry(session, "tab_owners")
    locks = _registry(session, "page_locks")
    owner = owners.get(tab_id)
    carried_lock = None
    for other, other_id in list(tab_ids.items()):
        if other_id != tab_id or other is page:
            continue
        del tab_ids[other]
        carried_lock = locks.pop(other, None) or carried_lock
        if not _is_closed(other):
            fresh = new_tab_id()
            tab_ids[other] = fresh
            if owner:
                owners[fresh] = owner
    previous = tab_ids.get(page)
    if previous is not None and previous != tab_id:
        owners.pop(previous, None)
        locks.pop(page, None)
    tab_ids[page] = tab_id
    if carried_lock is not None:
        locks[page] = carried_lock


def _is_closed(page: Any) -> bool:
    is_closed = getattr(page, "is_closed", None)
    if callable(is_closed):
        try:
            return is_closed() is True
        except Exception:
            return True
    return bool(getattr(page, "closed", False))


def page_for_tab(session: Any, tab_id: str, pages: Iterable[Any]) -> Any | None:
    """The live page carrying ``tab_id``, or None when that tab is gone.

    A tab whose page is a closed popup (sign-in window that closed itself)
    falls back to the page that opened it, like the active tab does.
    """
    session = unwrap_session(session)
    live = [page for page in pages if not _is_closed(page)]
    tab_ids = _registry(session, "tab_ids")
    for page in live:
        if tab_ids.get(page) == tab_id:
            return page
    for page, page_tab_id in list(tab_ids.items()):
        if page_tab_id != tab_id or not _is_closed(page):
            continue
        try:
            opener = session.popup_openers.get(page)
        except (AttributeError, TypeError):
            opener = None
        if opener is not None and any(opener is candidate for candidate in live):
            bind_tab(session, opener, tab_id)
            return opener
    return None


class _TabLock:
    """Shared session lock, then the tab's page lock; released in reverse."""

    def __init__(self, view: "TabView") -> None:
        self._view = view
        self._page_lock: asyncio.Lock | None = None

    async def __aenter__(self) -> None:
        real = self._view.real_session
        await real.lock.acquire_shared()
        try:
            page_lock = page_lock_for(real, self._view.page)
            await page_lock.acquire()
        except BaseException:
            real.lock.release_shared()
            raise
        self._page_lock = page_lock

    async def __aexit__(self, *exc_info: object) -> None:
        page_lock, self._page_lock = self._page_lock, None
        try:
            if page_lock is not None:
                page_lock.release()
        finally:
            self._view.real_session.lock.release_shared()

    def locked(self) -> bool:
        return page_lock_for(self._view.real_session, self._view.page).locked()


_VIEW_SLOTS = frozenset({"_real", "_page", "tab_id"})


class TabView:
    """One tab of a real ``BrowserSession``: reads and writes go to the real
    session, except ``page`` (this tab), ``lock`` (shared + tab lock) and
    ``mouse_position`` (kept per tab)."""

    tab_scoped = True

    def __init__(self, real: Any, page: Any, tab_id: str) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_page", page)
        object.__setattr__(self, "tab_id", tab_id)

    @property
    def real_session(self) -> Any:
        return self._real

    @property
    def page(self) -> Any:
        return self._page

    @property
    def lock(self) -> _TabLock:
        return _TabLock(self)

    @property
    def owner(self) -> str | None:
        return tab_owner(self._real, self.tab_id)

    @property
    def mouse_position(self) -> tuple[float, float] | None:
        try:
            return _registry(self._real, "page_mouse_positions").get(self._page)
        except TypeError:
            return None

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "page":
            self._rebind(value)
            return
        if name == "mouse_position":
            try:
                _registry(self._real, "page_mouse_positions")[self._page] = value
            except TypeError:
                pass
            return
        if name in _VIEW_SLOTS or name in {"lock", "real_session", "tab_scoped", "owner"}:
            raise AttributeError(f"TabView.{name} is read-only")
        setattr(self._real, name, value)

    def _rebind(self, new_page: Any) -> None:
        if new_page is self._page:
            return
        bind_tab(self._real, new_page, self.tab_id)
        object.__setattr__(self, "_page", new_page)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TabView session={getattr(self._real, 'id', '?')} tab={self.tab_id}>"

