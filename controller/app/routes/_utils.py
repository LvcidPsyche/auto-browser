from __future__ import annotations

import re
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from logging import Logger

from fastapi import HTTPException, Request
from pydantic import ValidationError

from ..action_errors import BROWSER_UNREACHABLE_DETAIL, BrowserActionError, BrowserUnreachableError
from ..approvals import CONTROLLER_SCOPE
from ..auth_policy import is_bearer_exempt_path
from ..browser.page_provenance import CLOSED_PAGE_GRACE_SECONDS
from ..browser.services.tabs import REFUSED_BY_ANOTHER_TAB_MESSAGE, REFUSED_TAB_MESSAGE, UNJUDGED_TAB_MESSAGE
from ..cron_service import CRON_STORE_UNREADABLE, CronStoreUnreadableError
from ..proxy_personas import (
    PROXY_PERSONA_DUPLICATE_ENTRIES,
    PROXY_PERSONA_FILE_UNREADABLE,
    ProxyPersonaDuplicateError,
    ProxyPersonaFileError,
)
from ..utils import MAX_LOGGED_PATH_CHARS, capped_for_log

_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def require_safe_segment(value: str, *, field: str) -> str:
    """Validate that *value* is a single safe path segment."""
    if not isinstance(value, str) or not _SAFE_PATH_SEGMENT.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")
    return value


TAKEOVER_LINK_ROUTE = "/sessions/{session_id}/takeover-link"

_HTTP_LINK_PREFIXES = ("http://", "https://")


def bounded_limit(limit: int, maximum: int) -> int:
    """A list's limit, at most ``maximum`` and never negative: 0 is an empty page, whichever backend serves it."""
    return max(0, min(limit, maximum))


def is_http_link(url: object) -> bool:
    """Whether a URL a page chose may be rendered as a clickable link: http(s) only, whatever its case or padding."""
    return isinstance(url, str) and url.strip().lower().startswith(_HTTP_LINK_PREFIXES)


async def require_session_access(request: Request) -> None:
    """Refuse another operator's session on every route that names one.

    Installed app-wide (app_factory), so routes that read a session's files
    directly (replay, witness, trace, network log) are covered as well as those
    that resolve the session through the manager. A query-string session_id
    counts too: GET /approvals?session_id=... must not list another operator's.
    """
    session_id = request.path_params.get("session_id") or request.query_params.get("session_id")
    manager = getattr(request.app.state, "browser_manager", None)
    # Controller-wide approvals are listed by their owner: their scope names no session.
    route = getattr(request.scope.get("route"), "path", None)
    listing_controller = route == "/approvals" and session_id == CONTROLLER_SCOPE
    # The takeover link checks its takeover token first and looks the session up itself (W2-19): a 404 from here
    # came before that check, so a caller without the token could tell which ids exist, unaudited.
    if route == TAKEOVER_LINK_ROUTE:
        return
    # A route the token gate lets through names no session it could check (W2-19's shape): a ?session_id= on /healthz
    # told any caller which ids exist. The docs pages read no session in any deployment, hence True.
    if is_bearer_exempt_path(str(request.scope.get("path") or ""), docs_are_public=True):
        return
    if session_id and manager is not None and not listing_controller:
        await manager.ensure_session_accessible(session_id)


# Fixed texts for a page or tab ALLOWED_HOSTS refuses. The PermissionError behind
# them names the host; these routes answer generically everywhere else.
OFF_LIST_PAGE_DETAIL = (
    "The page is on a host ALLOWED_HOSTS does not allow, or came from one, and is not read until it is "
    "navigated to an allowed URL. A page that loaded while another tab of this session was refused, or within "
    f"{CLOSED_PAGE_GRACE_SECONDS:g} seconds after a refused tab closed, is refused too: close the refused tab "
    "if it is still open, then navigate this one."
)
OFF_LIST_TAB_DETAIL = REFUSED_TAB_MESSAGE
OFF_LIST_OTHER_TAB_DETAIL = REFUSED_BY_ANOTHER_TAB_MESSAGE
UNJUDGED_TAB_DETAIL = UNJUDGED_TAB_MESSAGE
# What a route that reads the page, or activates a tab, passes typed_route_errors as its ``refusal``.
REFUSED_PAGE = (403, OFF_LIST_PAGE_DETAIL)
REFUSED_TAB = (403, OFF_LIST_TAB_DETAIL)


@contextmanager
def persona_file_errors() -> Iterator[None]:
    """Answer an unreadable proxy persona file as 503 with its fixed text (the path stays in the log), and a
    name the file holds more than one entry for as 409 with its own (the raw keys stay in the log)."""
    try:
        yield
    except ProxyPersonaFileError:
        raise HTTPException(status_code=503, detail=PROXY_PERSONA_FILE_UNREADABLE) from None
    except ProxyPersonaDuplicateError:
        raise HTTPException(status_code=409, detail=PROXY_PERSONA_DUPLICATE_ENTRIES) from None


@contextmanager
def cron_store_errors() -> Iterator[None]:
    """Answer an unreadable cron store as 503 with its fixed text (the path stays in the log)."""
    try:
        yield
    except CronStoreUnreadableError:
        raise HTTPException(status_code=503, detail=CRON_STORE_UNREADABLE) from None


@contextmanager
def unknown_id_is_404(key: str) -> Iterator[None]:
    """Answer the KeyError a service raises for an id it does not hold as the 404 it is, with no line in the log.

    ``key`` is what that KeyError carries: the id asked for (an approval's, a session record's, an auth profile's
    name as the service strips it) or the service's sentence about it (a cron job's). An id no record answers to
    raises it, and so does another operator's record where the service hides that one, and an approval's record
    that no longer validates. Nothing mapped it, so it reached the app's handler for a KeyError nothing maps
    (app/main.py), which answers this 404 and writes a WARNING with where the error was raised: for a caller's typo.
    Any other KeyError still goes there (a bug's, or a session that has gone, which keeps its own answer). The key
    holds the caller's id, as long as the server lets a request line be: the answer carries it cut, as that
    handler's does.
    """
    try:
        yield
    except KeyError as exc:
        if exc.args != (key,):
            raise
        raise HTTPException(status_code=404, detail=f"Not found: {capped_for_log(key)}") from None


def internal_error(logger: Logger, message: str, *args: object) -> HTTPException:
    logger.exception(message, *args)
    return HTTPException(status_code=500, detail="Internal error")


def queue_full(logger: Logger, refused: str, subject: str) -> HTTPException:
    """The 503 a full job queue is answered with, after one line in the log that says so.

    The HTTPException passes through typed_route_errors as it is, so nothing else logs the refusal. A warning with
    no traceback: nothing is broken, and the caller is told to try again.
    """
    logger.warning("the agent job queue is full: refused %s %s", refused, subject)
    return HTTPException(status_code=503, detail="Service unavailable")


# Of a hidden cause's traceback, the frames nearest the raise: they say where, and the route's message says which call.
HIDDEN_CAUSE_FRAMES = 3


def _on_one_line(value: object) -> str:
    """``value`` as a log line carries it: quoted, a line break in it escaped, and cut (capped_for_log).

    A str is quoted as it is. Anything else prints itself as it likes, and a str subclass answers repr() as it
    likes, so what repr() gave is quoted in turn, by str's own repr.
    """
    try:
        text = value if type(value) is str else repr(value)
        return str.__repr__(capped_for_log(text))
    except Exception:
        return f"<a {type(value).__name__} that could not be printed>"


def _cause_text(cause: Exception) -> str:
    """The error's own text for the log: on one line, cut, and never the values a model was given."""
    try:
        if isinstance(cause, ValidationError):
            # pydantic prints the value of each field it refused, and a body holds a webhook key or a password:
            # which field and what kind of refusal, no more.
            refused = cause.errors(include_url=False, include_context=False, include_input=False)
            fields = "; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}" for error in refused)
            text = f"{cause.error_count()} refused by {cause.title}: {fields}"
        else:
            text = str(cause)
        cut = "" if len(text) <= MAX_LOGGED_PATH_CHARS else f" (cut from {len(text)} characters)"
        return _on_one_line(text) + cut
    except Exception as failed:
        return f"<its text could not be made: {type(failed).__name__}>"


def log_hidden_cause(logger: Logger, status: int, cause: Exception, message: str, *args: object) -> None:
    """Log what a fixed answer hides: the route's message, the error, and where it was raised.

    A server-side fault raises the same three errors as a caller's mistake (a state file that no longer decodes is
    a ValueError, one the process may not read a PermissionError), and the fixed text told the operator nothing of
    it. A warning and not an error, since most of them are the caller's.

    The route's arguments and the error's text are the caller's as often as not (an id from the path, which the
    error then quotes), so each is written quoted and cut (_on_one_line; a number stays a number, for a %d): a
    line break in one starts no line of its own in the log, and a megabyte of path is not a megabyte of log. That
    is why the traceback is written here and not by exc_info, which prints the error's text as it is; of its
    frames only the last HIDDEN_CAUSE_FRAMES are kept, a line each: the file, the line and the function. A frame
    as the interpreter prints it is two lines on Python 3.12 and, from 3.13, as many as its call is written over,
    so the record would be as long as the Python it ran on made it.
    """
    shown = tuple(arg if isinstance(arg, (int, float)) else _on_one_line(arg) for arg in args)
    frames = traceback.extract_tb(cause.__traceback__, limit=-HIDDEN_CAUSE_FRAMES)
    where = "\n".join(f"  {frame.filename}:{frame.lineno} in {frame.name}" for frame in frames)
    text = _cause_text(cause)
    logger.warning(message + ": answered %d for %s: %s\n%s", *shown, status, type(cause).__name__, text, where)


@contextmanager
def typed_route_errors(
    logger: Logger, message: str, *args: object, refusal: tuple[int, str] | None = None
) -> Iterator[None]:
    """Answer a manager call's typed errors as themselves; log and hide the rest.

    A route that ends in ``except Exception: raise internal_error(...)`` catches
    before the app-level handlers in app/main.py run, so an unknown session or a
    ``BrowserActionError`` (a timeout, a missing element) came back as a 500
    "Internal error" and hid the ``code`` agents act on. Run the route body in
    this instead:

    - ``HTTPException`` (``ApprovalRequiredError`` included), ``BrowserActionError``
      and ``KeyError`` (``SessionNotFoundError`` included) pass through to the
      app handlers, which keep their status, ``code`` and payload.
    - ``PermissionError`` -> 403, ``ValueError`` -> 400, ``FileNotFoundError`` -> 404,
      ``BrowserUnreachableError`` -> 503. The texts are fixed: the exceptions' own messages can carry hosts and paths.
      The first three are logged with *message* before they are answered (log_hidden_cause).
    - Anything else is logged with *message* and answers 500 "Internal error".

    A route that answers the manager's refusals in words of its own (a 409 for a refused approval, the 403 of a
    refused page) passes them as ``refusal``, a status and a detail: every PermissionError the controller raised
    is answered so, and logged like the rest. One the operating system raised carries an errno (a file the
    process may not write) and is never that refusal: it keeps the generic 403, so a full or read-only disk is
    not answered as a conflict or as a page ALLOWED_HOSTS refuses.

    A route that maps one error's own class differently on purpose (a tab another tab refused, a busy session)
    catches it inside the ``with``: the HTTPException it raises passes through here untouched, and unlogged.
    """
    try:
        yield
    except (HTTPException, BrowserActionError, KeyError):
        raise
    except PermissionError as cause:
        status, detail = refusal if refusal is not None and cause.errno is None else (403, "Not permitted")
        log_hidden_cause(logger, status, cause, message, *args)
        raise HTTPException(status_code=status, detail=detail) from None
    except ValueError as cause:
        log_hidden_cause(logger, 400, cause, message, *args)
        raise HTTPException(status_code=400, detail="Invalid request") from None
    except FileNotFoundError as cause:
        log_hidden_cause(logger, 404, cause, message, *args)
        raise HTTPException(status_code=404, detail="Not found") from None
    except BrowserUnreachableError:
        raise HTTPException(status_code=503, detail=BROWSER_UNREACHABLE_DETAIL) from None
    except Exception:
        raise internal_error(logger, message, *args) from None
