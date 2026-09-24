from __future__ import annotations

from typing import Any


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

    def __init__(self, session_id: str, *, status: str | None = None) -> None:
        self.session_id = session_id
        self.status = status
        if status == "closed":
            self.code = "session_closed"
            message = f"Session {session_id} is closed. Create a new session, or list sessions to find a live one."
        elif status in {"interrupted", "failed"}:
            self.code = "session_interrupted"
            message = (
                f"Session {session_id} is {status}: its browser is gone (for example after a controller "
                "restart) and it cannot be resumed. Create a new session."
            )
        else:
            self.code = "unknown_session"
            message = f"No session with id {session_id}. List sessions to find a live one, or create a new session."
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:  # KeyError.__str__ would wrap the message in quotes
        return self.message
