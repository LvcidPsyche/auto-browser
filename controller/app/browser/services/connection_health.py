"""Is the controller's link to a browser still alive?

Two different things can die underneath a live session, and neither shows up
on its own as a Playwright "disconnected" event the session code would see:

- the Playwright *driver* (the Node process the Python client talks to over a
  pipe). When it exits, every Browser/Context/Page object in this controller
  is dead at once, `Browser.is_connected()` keeps returning True, and every
  call fails with "Connection closed while reading from the driver" or
  "unable to perform operation on <WriteUnixTransport closed=True>; the
  handler is closed". 2026-09-25: the driver died on an unhandled assertion
  (a CDP reply for a command it had already failed when the owner's tab
  crashed under browser-node's pid cap) and the session stayed "active" as a
  zombie while GET /sessions returned 500.
- the CDP connection to one browser (Chromium crashed, the relay was cut, the
  node was fenced). That one does surface as `Browser.is_connected()` False.

Everything here is a cheap, synchronous check so it can run on every
GET /sessions and on a short watchdog interval.
"""

from __future__ import annotations

import asyncio
from typing import Any

# Substrings of the errors a dead driver produces on the Python side.
_DRIVER_DEAD_MARKERS = (
    "connection closed while reading from the driver",
    "the handler is closed",
    "playwright connection closed",
)


def is_driver_dead_error(exc: BaseException | None) -> bool:
    """True when `exc` (or anything it was raised from) says the driver is gone."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        text = str(exc).lower()
        if any(marker in text for marker in _DRIVER_DEAD_MARKERS):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def playwright_driver_alive(playwright: Any) -> bool:
    """False only when the Python client positively knows its driver exited.

    Reads the client's own state (the transport's error future is resolved the
    moment the driver's stdout closes; `_closed_error` is set once the
    connection has been cleaned up). Anything it cannot read -- a different
    Playwright version, a test double -- counts as alive, so this can only
    ever add a detection, never invent one.
    """
    if playwright is None:
        return True
    connection = getattr(getattr(playwright, "_impl_obj", None), "_connection", None)
    if connection is None:
        return True
    if isinstance(getattr(connection, "_closed_error", None), BaseException):
        return False
    future = getattr(getattr(connection, "_transport", None), "on_error_future", None)
    if isinstance(future, asyncio.Future) and future.done():
        return False
    return True


def driver_exit_error(playwright: Any) -> str | None:
    """The dead driver's exit error, marked as retrieved (no asyncio
    "Future exception was never retrieved" noise); None if not dead/unknown."""
    connection = getattr(getattr(playwright, "_impl_obj", None), "_connection", None)
    future = getattr(getattr(connection, "_transport", None), "on_error_future", None)
    if isinstance(future, asyncio.Future) and future.done() and not future.cancelled():
        error = future.exception()
        return str(error) if error is not None else "driver stopped"
    return None


DRIVER_EXITED = "the controller's Playwright driver exited"


def session_connection_problem(manager: Any, session: Any) -> str | None:
    """Why this live session can no longer reach its browser, or None."""
    if not playwright_driver_alive(getattr(manager, "playwright", None)):
        return DRIVER_EXITED
    manager_epoch = getattr(manager, "_driver_epoch", None)
    session_epoch = getattr(session, "driver_epoch", None)
    if isinstance(manager_epoch, int) and isinstance(session_epoch, int) and manager_epoch != session_epoch:
        # The driver was restarted since this session attached: its handles
        # belong to the dead one (and still claim is_connected()).
        return DRIVER_EXITED
    browser = getattr(session, "browser", None)
    is_connected = getattr(browser, "is_connected", None)
    if browser is not None and callable(is_connected):
        try:
            connected = is_connected()
        except Exception:  # pragma: no cover - defensive: a broken handle is a dead one
            connected = False
        if connected is False:
            return "the browser connection closed (browser crashed, relay cut, or node fenced)"
    return None
