"""A session-wide reader/writer lock that is a drop-in for ``asyncio.Lock``.

``async with session.lock:`` keeps meaning what it always meant: the caller
has the whole session to itself (open/activate/close a tab, close the session,
re-attach a dead browser link, save an auth profile, any action that is not
scoped to one tab). ``async with session.lock.shared():`` is the other side:
several holders at once -- tab-scoped actions, each of which also takes its
own tab's page lock (see ``app/browser/tab_view.py``), so employees working in
different tabs run side by side while nothing session-wide can start under
them.

Writer preference: once an exclusive acquirer is queued, new shared acquirers
queue behind it, so a close/revoke can never be starved by a stream of tab
actions. Waiters are served in arrival order. Exclusive is not re-entrant
(exactly like ``asyncio.Lock``). A waiter cancelled at any point -- before or
just after it was granted -- leaves the counters exact and wakes whoever can
now proceed.
"""

from __future__ import annotations

import asyncio
import collections
from contextlib import asynccontextmanager
from typing import AsyncIterator


class SessionLock:
    def __init__(self) -> None:
        self._readers = 0
        self._writer = False
        # (exclusive?, future) in arrival order.
        self._waiters: collections.deque[tuple[bool, asyncio.Future[bool]]] = collections.deque()

    # --- asyncio.Lock compatible (exclusive) ----------------------------------------

    def locked(self) -> bool:
        return self._writer or self._readers > 0

    async def acquire(self) -> bool:
        if not self._writer and self._readers == 0 and not self._pending():
            self._writer = True
            return True
        await self._wait(exclusive=True)
        return True

    def release(self) -> None:
        if not self._writer:
            raise RuntimeError("SessionLock is not held exclusively")
        self._writer = False
        self._wake()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *exc_info: object) -> None:
        self.release()

    # --- shared side --------------------------------------------------------------------

    async def acquire_shared(self) -> None:
        if not self._writer and not self._pending():
            self._readers += 1
            return
        await self._wait(exclusive=False)

    def release_shared(self) -> None:
        if self._readers <= 0:
            raise RuntimeError("SessionLock is not held shared")
        self._readers -= 1
        self._wake()

    @asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        await self.acquire_shared()
        try:
            yield
        finally:
            self.release_shared()

    # --- introspection (tests / diagnostics) -------------------------------------------

    @property
    def shared_holders(self) -> int:
        return self._readers

    @property
    def exclusive_waiting(self) -> int:
        return sum(1 for exclusive, fut in self._waiters if exclusive and not fut.done())

    # --- internals -----------------------------------------------------------------------

    def _pending(self) -> bool:
        return any(not fut.done() for _exclusive, fut in self._waiters)

    async def _wait(self, *, exclusive: bool) -> None:
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        entry = (exclusive, fut)
        self._waiters.append(entry)
        try:
            await fut
        except BaseException:
            try:
                self._waiters.remove(entry)
            except ValueError:
                pass
            if fut.done() and not fut.cancelled():
                # Granted, then cancelled before we resumed: give it back.
                if exclusive:
                    self._writer = False
                else:
                    self._readers -= 1
            # Whoever was queued behind us may be able to go now.
            self._wake()
            raise

    def _wake(self) -> None:
        while self._waiters:
            exclusive, fut = self._waiters[0]
            if fut.done():  # cancelled waiter
                self._waiters.popleft()
                continue
            if exclusive:
                if self._writer or self._readers:
                    return
                self._waiters.popleft()
                self._writer = True
                fut.set_result(True)
                return
            if self._writer:
                return
            self._waiters.popleft()
            self._readers += 1
            fut.set_result(True)
            # keep granting consecutive shared waiters
