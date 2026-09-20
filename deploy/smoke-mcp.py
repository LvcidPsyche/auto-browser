"""Use a real MCP client SDK against the broker through a private SSH tunnel."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def remote_env(name: str) -> str:
    key_path = Path.home() / ".ssh" / "stone_main"
    result = subprocess.run(
        [
            "ssh", "-i", str(key_path), "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "root@204.168.150.160",
            f"grep '^{name}=' /opt/auto-browser/.env",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().split("=", 1)[1]


def owner_decision(owner_token: str, request_id: str, decision: str, body: dict | None = None) -> None:
    encoded = None if body is None else json.dumps(body).encode()
    request = Request(
        f"http://127.0.0.1:18001/requests/{request_id}/{decision}",
        data=encoded or b"",
        headers={"Authorization": f"Bearer {owner_token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"Owner {decision} returned {response.status}")


async def main() -> None:
    agent_entry = remote_env("BROKER_AGENT_TOKENS").split(",", 1)[0]
    agent_token = agent_entry.split(":", 1)[1]
    owner_token = remote_env("BROKER_OWNER_TOKEN")
    headers = {"Authorization": f"Bearer {agent_token}"}
    async with streamablehttp_client("http://127.0.0.1:18001/mcp", headers=headers) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            required = {"browser.request_access", "browser.get_request", "browser.create_session", "browser.observe", "browser.complete"}
            if not required.issubset(names):
                raise RuntimeError(f"Missing MCP tools: {required - names}")
            print(f"PASS standard MCP client initialized and listed {len(names)} tools")
            requested = await session.call_tool("browser.request_access", {
                "profile": "pilot_example",
                "start_url": "https://example.com",
                "purpose": "Non-sensitive MCP smoke test",
            })
            request_id = json.loads(requested.content[0].text)["id"]
            owner_decision(owner_token, request_id, "approve", {})
            try:
                opened = await session.call_tool("browser.create_session", {"request_id": request_id})
                if not json.loads(opened.content[0].text).get("id"):
                    raise RuntimeError("MCP browser session was not created")
                observed = await session.call_tool("browser.observe", {"request_id": request_id})
                if observed.isError:
                    raise RuntimeError("MCP browser observation failed")
                navigated = await session.call_tool("browser.navigate", {
                    "request_id": request_id,
                    "arguments": {"url": "https://example.com/"},
                })
                if navigated.isError:
                    raise RuntimeError("MCP browser navigation failed")
                print("PASS MCP tool call opened, observed, and navigated an approved browser session")
                completed = await session.call_tool("browser.complete", {"request_id": request_id})
                if json.loads(completed.content[0].text)["status"] != "completed":
                    raise RuntimeError("MCP task completion did not close its session")
                print("PASS MCP agent completed task and closed its session")
            finally:
                owner_decision(owner_token, request_id, "revoke")


if __name__ == "__main__":
    asyncio.run(main())
