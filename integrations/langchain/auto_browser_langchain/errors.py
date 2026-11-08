from __future__ import annotations

from typing import Any

import httpx

# httpx's words for each class of status raise_for_status refuses.
_STATUS_KINDS = {1: "Informational response", 3: "Redirect response", 4: "Client error", 5: "Server error"}


def raise_for_status(response: httpx.Response) -> None:
    """Raise httpx.HTTPStatusError for a response that is not 2xx, as ``response.raise_for_status()`` does and in its
    words, but naming the request URL without its userinfo, query or fragment (R5C-20).

    httpx's message names the URL whole, the password, a token in the query and the fragment included, and an agent
    shown the error (LangGraph's ToolNode hands it the exception's text) read them.
    """
    if response.is_success:
        return
    kind = _STATUS_KINDS.get(response.status_code // 100, "Invalid status code")
    url = response.request.url.copy_with(userinfo=b"", query=None, fragment=None)
    raise httpx.HTTPStatusError(
        f"{kind} '{response.status_code} {response.reason_phrase}' for url '{url}'\n"
        f"For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/{response.status_code}",
        request=response.request,
        response=response,
    )


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
