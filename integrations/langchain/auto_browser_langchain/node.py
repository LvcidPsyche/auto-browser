from __future__ import annotations

import json
from typing import Any, TypedDict

import httpx

from .errors import AutoBrowserToolError


class BrowserState(TypedDict, total=False):
    session_id: str
    current_url: str
    screenshot_url: str
    goal: str
    result: str
    error: str


def _tool_error(name: str, result: dict[str, Any]) -> AutoBrowserToolError:
    content = result.get("content") or [{}]
    text = str(content[0].get("text") or "Unknown error")
    structured = result.get("structuredContent")
    payload = structured if isinstance(structured, dict) else None
    if payload is None:
        # An older controller sends the payload only as JSON text.
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        payload = parsed if isinstance(parsed, dict) else None
    message = payload.get("error") if payload else None
    return AutoBrowserToolError(name, message if isinstance(message, str) else text, payload)


def _tool_data(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """The tool's result as a dict; raises AutoBrowserToolError for an isError response.

    A successful result is normally JSON in content[0].text. Text that is not
    JSON (or is JSON but not an object) comes back wrapped, as {"text": ...} or
    {"result": ...}, rather than failing the parse.
    """
    if result.get("isError"):
        raise _tool_error(name, result)
    content = result.get("content") or [{}]
    text = content[0].get("text", "{}")
    try:
        data = json.loads(text)
    except ValueError:
        return {"text": text}
    return data if isinstance(data, dict) else {"result": data}


class AutoBrowserNode:
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        bearer_token: str | None = None,
        timeout: float = 60.0,
        operator_id: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout = timeout
        self.operator_id = operator_id

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        if self.operator_id:
            headers["X-Operator-Id"] = self.operator_id
        return headers

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post(
                "/mcp/tools/call",
                json={"name": name, "arguments": arguments},
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    async def create_session(self, start_url: str | None = None) -> str:
        arguments: dict[str, Any] = {}
        if start_url:
            arguments["start_url"] = start_url
        result = await self.call_tool("browser.create_session", arguments)
        data = _tool_data("browser.create_session", result)
        session_id = data.get("session_id") or data.get("id")
        if not session_id:
            raise KeyError("browser.create_session did not return a session id")
        return str(session_id)

    async def observe(self, session_id: str) -> dict[str, Any]:
        result = await self.call_tool("browser.observe", {"session_id": session_id})
        return _tool_data("browser.observe", result)

    async def run(self, state: BrowserState) -> BrowserState:
        session_id = state.get("session_id")
        if not session_id:
            session_id = await self.create_session()
            state = {**state, "session_id": session_id}
        observation = await self.observe(session_id)
        return {
            **state,
            "current_url": observation.get("url", ""),
            "screenshot_url": observation.get("screenshot_url", ""),
        }
