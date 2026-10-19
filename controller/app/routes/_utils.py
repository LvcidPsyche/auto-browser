from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from logging import Logger

from fastapi import HTTPException, Request

from ..action_errors import BrowserActionError

_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def require_safe_segment(value: str, *, field: str) -> str:
    """Validate that *value* is a single safe path segment."""
    if not isinstance(value, str) or not _SAFE_PATH_SEGMENT.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")
    return value


async def require_session_access(request: Request) -> None:
    """Refuse another operator's session on every route that names one.

    Installed app-wide (app_factory), so routes that read a session's files
    directly (replay, witness, trace, network log) are covered as well as those
    that resolve the session through the manager. A query-string session_id
    counts too: GET /approvals?session_id=... must not list another operator's.
    """
    session_id = request.path_params.get("session_id") or request.query_params.get("session_id")
    manager = getattr(request.app.state, "browser_manager", None)
    if session_id and manager is not None:
        await manager.ensure_session_accessible(session_id)


# Fixed texts for a page or tab ALLOWED_HOSTS refuses. The PermissionError behind
# them names the host; these routes answer generically everywhere else.
OFF_LIST_PAGE_DETAIL = (
    "The page is on a host ALLOWED_HOSTS does not allow and is being moved to about:blank. Try again in a moment."
)
OFF_LIST_TAB_DETAIL = "That tab is on a host ALLOWED_HOSTS does not allow."


def internal_error(logger: Logger, message: str, *args: object) -> HTTPException:
    logger.exception(message, *args)
    return HTTPException(status_code=500, detail="Internal error")


@contextmanager
def typed_route_errors(logger: Logger, message: str, *args: object) -> Iterator[None]:
    """Answer a manager call's typed errors as themselves; log and hide the rest.

    A route that ends in ``except Exception: raise internal_error(...)`` catches
    before the app-level handlers in app/main.py run, so an unknown session or a
    ``BrowserActionError`` (a timeout, a missing element) came back as a 500
    "Internal error" and hid the ``code`` agents act on. Run the route body in
    this instead:

    - ``HTTPException`` (``ApprovalRequiredError`` included), ``BrowserActionError``
      and ``KeyError`` (``SessionNotFoundError`` included) pass through to the
      app handlers, which keep their status, ``code`` and payload.
    - ``PermissionError`` -> 403, ``ValueError`` -> 400, ``FileNotFoundError`` -> 404.
      The texts are fixed: the exceptions' own messages can carry hosts and paths.
    - Anything else is logged with *message* and answers 500 "Internal error".

    A route that maps an error differently on purpose (a 409 for a refused
    approval, the host-specific 403) catches it inside the ``with``: the
    HTTPException it raises passes through here untouched.
    """
    try:
        yield
    except (HTTPException, BrowserActionError, KeyError):
        raise
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request") from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Not found") from None
    except Exception:
        raise internal_error(logger, message, *args) from None
