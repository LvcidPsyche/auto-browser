from __future__ import annotations

import json
from typing import Any, Optional, Type

import httpx

try:
    from langchain_core.tools import BaseTool
    from pydantic import BaseModel
    from pydantic import Field as PydanticField

    _LANGCHAIN_AVAILABLE = True
except ImportError:
    _LANGCHAIN_AVAILABLE = False
    BaseTool = object
    BaseModel = object
    PydanticField = lambda *args, **kwargs: None  # noqa: E731


if _LANGCHAIN_AVAILABLE:

    class AutoBrowserInput(BaseModel):
        action: str = PydanticField(description="MCP tool name, e.g. 'browser.observe'")
        arguments: dict[str, Any] = PydanticField(default_factory=dict, description="Tool arguments")
else:
    AutoBrowserInput = None  # type: ignore[assignment]


class AutoBrowserTool(BaseTool):
    name: str = "auto_browser"
    description: str = (
        "Control a real browser via auto-browser. "
        "Use 'action' for the MCP tool name (e.g. 'browser.navigate', 'browser.observe', "
        "'browser.click') and 'arguments' for its parameters. "
        "Call browser.create_session first to get a session_id."
    )
    if _LANGCHAIN_AVAILABLE:
        args_schema: Type[AutoBrowserInput] = AutoBrowserInput

    base_url: str = "http://localhost:8000"
    bearer_token: Optional[str] = None
    timeout: float = 60.0

    def _run(self, action: str, arguments: dict[str, Any] | None = None) -> str:
        import asyncio

        # asyncio.run, not get_event_loop().run_until_complete(): since Python 3.14
        # get_event_loop() raises RuntimeError when no loop is running, which broke
        # this sync path outright on a version the package claims to support.
        # Callers already inside a running loop should await _arun via ainvoke.
        return asyncio.run(self._arun(action, arguments or {}))

    async def _arun(self, action: str, arguments: dict[str, Any] | None = None) -> str:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        payload = {"name": action, "arguments": arguments or {}}
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post("/mcp/tools/call", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        if data.get("isError"):
            content = data.get("content") or [{}]
            return f"ERROR: {content[0].get('text', 'Unknown error')}"
        content = data.get("content") or [{}]
        return content[0].get("text", json.dumps(data))

    @classmethod
    def list_tools(
        cls,
        base_url: str = "http://localhost:8000",
        bearer_token: str | None = None,
    ) -> list[dict]:
        headers: dict[str, str] = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        with httpx.Client(base_url=base_url, timeout=10) as client:
            response = client.get("/mcp/tools", headers=headers)
            response.raise_for_status()
            return response.json()
