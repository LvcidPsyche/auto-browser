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
- a **stdio bridge**, installable from PyPI (`uvx auto-browser-mcp`) or bundled at `scripts/mcp_stdio_bridge.py` for repo checkouts

That means:
- MCP clients with HTTP transport support can talk to it directly
- stdio-first clients can use the bridge with zero setup beyond `uvx`

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

Minimal shape (requires [uv](https://docs.astral.sh/uv/)):

```json
{
  "mcpServers": {
    "auto-browser": {
      "command": "uvx",
      "args": ["auto-browser-mcp"],
      "env": {
        "AUTO_BROWSER_BASE_URL": "http://127.0.0.1:8000/mcp",
        "AUTO_BROWSER_BEARER_TOKEN": ""
      }
    }
  }
}
```

Working from a repo checkout without uv? Point `command` at `python3` with
`args: ["/ABSOLUTE/PATH/TO/auto-browser/scripts/mcp_stdio_bridge.py"]` instead.

If your API is protected, set `AUTO_BROWSER_BEARER_TOKEN`.

## Recommended first demo

The best MCP demo is:

1. create session
2. navigate/login manually once if needed
3. save auth profile
4. open a second session from that auth profile
5. continue work through MCP tools

That shows why MCP + browser state reuse is more valuable than a plain “open page and click things” demo.

## Resources and subscriptions

MCP clients can inspect browser state through resources as well as one-shot
tools. Start with a normal MCP initialization, keep the returned
`MCP-Session-Id` and `MCP-Protocol-Version` headers, then send
`notifications/initialized`.

Minimal resource listing over the HTTP transport:

```bash
curl -s http://127.0.0.1:8000/mcp \
  -X POST \
  -H 'accept: application/json, text/event-stream' \
  -H 'content-type: application/json' \
  -H "MCP-Session-Id: $MCP_SESSION_ID" \
  -H "MCP-Protocol-Version: $MCP_PROTOCOL_VERSION" \
  -d '{
    "jsonrpc": "2.0",
    "id": 20,
    "method": "resources/list",
    "params": {}
  }' | jq
```

The default resource set includes:

- `browser://sessions` for active browser sessions
- `browser://audit/events` for recent audit events
- `browser://<session-id>/dom`, `browser://<session-id>/screenshot`,
  `browser://<session-id>/console`, and `browser://<session-id>/network` once a
  session exists

Read a resource by URI:

```bash
curl -s http://127.0.0.1:8000/mcp \
  -X POST \
  -H 'accept: application/json, text/event-stream' \
  -H 'content-type: application/json' \
  -H "MCP-Session-Id: $MCP_SESSION_ID" \
  -H "MCP-Protocol-Version: $MCP_PROTOCOL_VERSION" \
  -d '{
    "jsonrpc": "2.0",
    "id": 21,
    "method": "resources/read",
    "params": {
      "uri": "browser://audit/events"
    }
  }' | jq
```

Clients that support resource updates can subscribe, then keep the MCP event
stream open with `GET /mcp`:

```bash
curl -s http://127.0.0.1:8000/mcp \
  -X POST \
  -H 'accept: application/json, text/event-stream' \
  -H 'content-type: application/json' \
  -H "MCP-Session-Id: $MCP_SESSION_ID" \
  -H "MCP-Protocol-Version: $MCP_PROTOCOL_VERSION" \
  -d '{
    "jsonrpc": "2.0",
    "id": 22,
    "method": "resources/subscribe",
    "params": {
      "uri": "browser://sessions"
    }
  }' | jq

curl -N http://127.0.0.1:8000/mcp \
  -H 'accept: text/event-stream' \
  -H "MCP-Session-Id: $MCP_SESSION_ID" \
  -H "MCP-Protocol-Version: $MCP_PROTOCOL_VERSION"
```

When a subscribed resource changes, the stream emits a JSON-RPC notification
named `notifications/resources/updated` with the updated `uri`.

## Curated vs full MCP tool profile

The default MCP tool profile is `curated`.

That hides:
- approval admin tools
- built-in agent queue tools
- provider introspection tools
- remote-access admin tools

Why:
- smaller tool surface
- better tool selection quality for LLMs
- clearer product identity: MCP server first, optional agent runner second

If you really want the whole surface:

```bash
MCP_TOOL_PROFILE=full
```

In the full profile, `browser.queue_agent_run` accepts `workflow_profile`
(`fast` or `governed`) and `browser.resume_agent_job` resumes interrupted,
failed, or step-limited background runs from persisted checkpoints.

The full profile also exposes the convergence harness:

- `harness.start_convergence`
- `harness.get_status`
- `harness.get_trace`
- `harness.list_runs`
- `harness.list_candidates`
- `harness.get_candidate`
- `harness.graduate`

Use `workflow_profile=governed` when starting convergence against a live browser session. `harness.graduate` writes only a staged candidate; promotion to the production skill corpus remains a separate governed review step.

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
