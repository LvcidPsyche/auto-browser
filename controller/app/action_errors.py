from __future__ import annotations

from typing import Any

# PII_SCRUB_SCREENSHOT_STRICT refused to return a screenshot it could not scrub.
SCREENSHOT_WITHHELD = "screenshot_withheld"
# The page's isolated world did not answer a message within its deadline: the renderer may have crashed or hung.
PAGE_NOT_RESPONDING = "page_not_responding"
# A policy refusal (ALLOWED_HOSTS, the witness policy, a navigation stopped before it was sent): 403, not retryable.
BROWSER_ACTION_BLOCKED = "browser_action_blocked"
# No pre-request gate on the browser (app/browser/request_gate.py): 503, retryable, the next session attaches one.
PRE_REQUEST_GATE_UNAVAILABLE = "pre_request_gate_unavailable"


class RefusedPageError(PermissionError):
    """A page ALLOWED_HOSTS refuses, for its URL or where it came from, is not read (the page checks).

    A PermissionError still, for every caller that catches one; a route that answers other
    PermissionErrors generically tells this one apart (the fork route).
    """


class ErrorPageSourceError(ValueError):
    """Fork or shadow browse from Chrome's error page with no start_url: that page is no page to open. A
    ValueError (a 400) whose fixed text the routes return as it is: it names no host or path."""


ERROR_PAGE_SOURCE_FORK = (
    "The session's page is Chrome's error page, for a navigation that was stopped or failed, so there is no page "
    "to fork onto. Pass start_url."
)
ERROR_PAGE_SOURCE_SHADOW = (
    "The session's page is Chrome's error page, for a navigation that was stopped or failed, so there is no page "
    "to open in the shadow browser. Navigate the session to a page first."
)


# Why the controller itself closed a session (SessionRecord.close_reason): its pre-request gate was lost.
SESSION_CLOSED_GATE_LOST = "request_gate_lost"


class NavigationStoppedError(PermissionError):
    """A navigation the pre-request gate stopped before it was sent. The text names only the refused origin, which
    ``origin`` carries for the error's ``url`` (None when no block could be tied to the page)."""

    def __init__(self, message: str, *, origin: str | None = None) -> None:
        super().__init__(message)
        self.origin = origin


class BrowserActionError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "browser_action_failed",
        action: str | None = None,
        status_code: int = 400,
        retryable: bool | None = None,
        url: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.action = action
        self.status_code = status_code
        self.retryable = retryable
        self.url = url
        self.details = details or {}

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.message,
            "code": self.code,
            "action": self.action,
            "retryable": self.retryable,
            "url": self.url,
            **self.details,
        }


class SessionNotFoundError(KeyError):
    """A session id that names no live session, with a reason a caller can act on.

    Subclasses KeyError so every existing ``except KeyError`` (REST 404s, the
    share routes, the MCP gateway) keeps working. The plain ``KeyError(id)`` it
    replaces surfaced to agents as an error whose entire text was the id.
    """

    def __init__(self, session_id: str, *, status: str | None = None, reason: str | None = None) -> None:
        self.session_id = session_id
        self.status = status
        self.reason = reason
        if status == "closed" and reason == SESSION_CLOSED_GATE_LOST:
            self.code = "session_closed"
            message = (
                f"Session {session_id} was closed by the controller: its pre-request gate detached from the "
                "browser, so its pages could no longer be stopped before loading a host ALLOWED_HOSTS does not "
                "allow. Create a new session."
            )
        elif status == "closed":
            self.code = "session_closed"
            message = f"Session {session_id} is closed. Create a new session, or list sessions to find a live one."
        elif status in {"interrupted", "failed"}:
            self.code = "session_interrupted"
            message = (
                f"Session {session_id} is {status}: its browser is gone (for example after a controller "
                "restart) and it cannot be resumed. Create a new session."
            )
        elif status is not None:
            # Recorded as active but not running here: with a shared session
            # store it belongs to another controller instance, otherwise it
            # stopped without being recorded as closed.
            self.code = "session_not_live"
            message = (
                f"Session {session_id} is recorded as {status} but is not running on this controller "
                "(it may belong to another controller instance). Create a new session, or list sessions "
                "to find a live one."
            )
        else:
            self.code = "unknown_session"
            message = f"No session with id {session_id}. List sessions to find a live one, or create a new session."
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:  # KeyError.__str__ would wrap the message in quotes
        return self.message
