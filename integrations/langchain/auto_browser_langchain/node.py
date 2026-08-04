from __future__ import annotations

import json
from typing import Any, TypedDict

import httpx


class BrowserState(TypedDict, total=False):
    session_id: str
    current_url: str
    screenshot_url: str
    goal: str
    result: str
    error: str


class AutoBrowserNode:
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        bearer_token: str | None = None,
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
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
        content = result.get("content") or [{}]
        data = json.loads(content[0].get("text", "{}"))
        session_id = data.get("session_id") or data.get("id")
        if not session_id:
            raise KeyError("browser.create_session did not return a session id")
        return str(session_id)

    async def observe(self, session_id: str) -> dict[str, Any]:
        result = await self.call_tool("browser.observe", {"session_id": session_id})
        content = result.get("content") or [{}]
        return json.loads(content[0].get("text", "{}"))

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
