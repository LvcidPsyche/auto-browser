# MCP Client Integration

Auto Browser exposes a real MCP transport at:

```text
http://127.0.0.1:8000/mcp
```

It also exposes convenience endpoints at:

```text
http://127.0.0.1:8000/mcp/tools
http://127.0.0.1:8000/mcp/tools/call
```

## Transport model

Auto Browser exposes:

- an **HTTP MCP server**
- a bundled **stdio bridge** at `scripts/mcp_stdio_bridge.py`

That means:
- MCP clients with HTTP transport support can talk to it directly
- stdio-first clients can use the shipped bridge directly

## Why this matters

Most browser automation projects are just scripts or raw APIs.

Auto Browser is interesting because it already packages the browser layer as an MCP-native tool server with:
- session lifecycle
- observations
- tool calls
- approvals
- auth profile reuse
- human takeover

## Recommended local setup

1. start Auto Browser locally
2. confirm `make doctor` passes
3. point your MCP-capable client at `/mcp`
4. use the browser tools for:
   - session create
   - observe
   - actions
   - auth profile save/reuse

## Claude Desktop example

Auto Browser ships a copy-paste Claude Desktop config example:

- `examples/claude_desktop_config.json`
- `examples/claude-desktop-setup.md`

Minimal shape:

```json
{
  "mcpServers": {
    "auto-browser": {
      "command": "python3",
      "args": [
        "/ABSOLUTE/PATH/TO/auto-browser/scripts/mcp_stdio_bridge.py"
      ],
      "env": {
        "AUTO_BROWSER_BASE_URL": "http://127.0.0.1:8000/mcp",
        "AUTO_BROWSER_BEARER_TOKEN": ""
      }
    }
  }
}
```

If your API is protected, set `AUTO_BROWSER_BEARER_TOKEN`.

## Recommended first demo

The best MCP demo is:

1. create session
2. navigate/login manually once if needed
3. save auth profile
4. open a second session from that auth profile
5. continue work through MCP tools

That shows why MCP + browser state reuse is more valuable than a plain “open page and click things” demo.

## Curated vs full MCP tool profile

The default MCP tool profile is `curated`.

That hides:
- approval admin tools
- built-in agent queue tools
- provider introspection tools
- remote-access admin tools
- most social write convenience tools

Why:
- smaller tool surface
- better tool selection quality for LLMs
- clearer product identity: MCP server first, optional agent runner second

If you really want the whole surface:

```bash
MCP_TOOL_PROFILE=full
```

## Raw tool-call example

If you want to see the shape of the tool surface without wiring up a full MCP client yet:

```bash
curl -s http://127.0.0.1:8000/mcp/tools | jq

curl -s http://127.0.0.1:8000/mcp/tools/call \
  -X POST \
  -H 'content-type: application/json' \
  -d '{
    "name": "browser.create_session",
    "arguments": {
      "name": "demo",
      "start_url": "https://example.com"
    }
  }' | jq
```
