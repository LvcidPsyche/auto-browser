"""Fake CDP sessions for the pre-request gate (app/browser/request_gate.py).

A browser-level session (Browser.new_browser_cdp_session) and a page-level one
(BrowserContext.new_cdp_session), as far as the gate uses them: send() records each command and
answers from `replies`, on() keeps handlers the way Playwright's CDPSession (a pyee emitter) does,
and emit() delivers an event to them inline, as Playwright does.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import defaultdict
from typing import Any, Callable

_IDS = itertools.count(1)
# How long settle_background waits for the controller's background tasks (containment, the gate's handlers).
SETTLE_DEADLINE_SECONDS = 5.0
SETTLE_EXTRA_YIELDS = 5


async def settle_background() -> None:
    """Wait for the controller's background tasks (app.utils.spawn_background_task: gate containment, the gate's
    request handlers) to finish, including the ones they spawn, then yield a few more times for their callbacks.

    A fixed number of yields raced them: containment has more awaits since round 4, and Python 3.11's
    asyncio.wait_for takes more loop iterations than 3.12+, so tests read before it finished (CI, 3.11).
    A task still pending at the deadline is left running; the test's own assertion then says what is missing.
    """
    from app.utils import _BACKGROUND_TASKS

    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE_DEADLINE_SECONDS
    current = asyncio.current_task()
    while True:
        await asyncio.sleep(0)  # let what was just scheduled register itself first
        pending = {task for task in _BACKGROUND_TASKS if not task.done() and task is not current}
        remaining = deadline - loop.time()
        if not pending or remaining <= 0:
            break
        await asyncio.wait(pending, timeout=remaining)
    for _ in range(SETTLE_EXTRA_YIELDS):
        await asyncio.sleep(0)


class FakeCDPSession:
    def __init__(self, replies: dict[str, Any] | None = None, *, log: list[Any] | None = None) -> None:
        # method -> a result dict, an exception to raise, or a callable(params) returning either.
        self.replies: dict[str, Any] = dict(replies or {})
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, list[Callable[[Any], Any]]] = defaultdict(list)
        self.detached = False
        # Shared with whatever else a test orders against (a browser's new_context calls).
        self.log = log if log is not None else []

    def on(self, event: str, handler: Callable[[Any], Any]) -> None:
        self.handlers[event].append(handler)

    def emit(self, event: str, payload: Any = None) -> None:
        for handler in list(self.handlers[event]):
            handler(payload)

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        self.sent.append((method, params))
        self.log.append(("cdp", method))
        reply = self.replies.get(method, {})
        if callable(reply):
            reply = reply(params)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    async def detach(self) -> None:
        self.detached = True
        self.emit("close", self)

    def replies_to(self, request_id: str) -> list[tuple[str, dict[str, Any]]]:
        return [
            (method, params)
            for method, params in self.sent
            if method in ("Fetch.continueRequest", "Fetch.failRequest") and params.get("requestId") == request_id
        ]

    async def wait_for_reply(self, request_id: str, *, timeout: float = 2.0) -> tuple[str, dict[str, Any]]:
        """The one reply the gate sent for a paused request, waiting for its task to run."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            replies = self.replies_to(request_id)
            if replies:
                await asyncio.sleep(0)
                replies = self.replies_to(request_id)
                assert len(replies) == 1, f"{request_id} was answered {len(replies)} times: {replies}"
                return replies[0]
            await asyncio.sleep(0.001)
        raise AssertionError(f"the gate never answered {request_id}")


def page_session_for(context_id: str, target_id: str | None = None, *, log: list[Any] | None = None) -> FakeCDPSession:
    """A page's CDP session, answering Target.getTargetInfo as Chromium does for a page target."""
    return FakeCDPSession(
        {
            "Target.getTargetInfo": {
                "targetInfo": {
                    "targetId": target_id or f"page-{next(_IDS)}",
                    "type": "page",
                    "browserContextId": context_id,
                    "url": "about:blank",
                }
            }
        },
        log=log,
    )


def new_context_id() -> str:
    return f"ctx-{next(_IDS)}"
