from __future__ import annotations

from typing import Any


class AutoBrowserToolError(RuntimeError):
    """A tool call the controller answered with ``isError: true``.

    /mcp/tools/call reports a failed tool as HTTP 200, so ``raise_for_status``
    never sees it. ``payload`` is the controller's ``structuredContent`` when it
    sent an object, e.g. ``{"error": ..., "code": "selector_not_found",
    "retryable": False}`` or ``{"error": ..., "code": "unknown_session"}``; it is
    None for a bare text error such as "Tool execution failed". Check ``code``
    to tell a missing session from a bad selector.
    """

    def __init__(self, tool: str, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(f"{tool} failed: {message}")
        self.tool = tool
        self.message = message
        self.payload = payload

    @property
    def code(self) -> str | None:
        value = (self.payload or {}).get("code")
        return value if isinstance(value, str) else None

    @property
    def retryable(self) -> bool | None:
        value = (self.payload or {}).get("retryable")
        return value if isinstance(value, bool) else None
