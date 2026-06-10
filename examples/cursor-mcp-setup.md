# Cursor MCP setup

## 1. Start Auto Browser

```bash
docker compose up --build
```

## 2. Open Cursor MCP settings

In Cursor: **Settings → MCP** (or open `~/.cursor/mcp.json` directly).

## 3. Add this config block

With [uv](https://docs.astral.sh/uv/) installed (recommended — no clone needed):

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

Or from a repo checkout, replace `<ABSOLUTE_PATH_TO_AUTO_BROWSER>` with the real path to your clone:

```json
{
  "mcpServers": {
    "auto-browser": {
      "command": "python3",
      "args": [
        "<ABSOLUTE_PATH_TO_AUTO_BROWSER>/scripts/mcp_stdio_bridge.py"
      ],
      "env": {
        "AUTO_BROWSER_BASE_URL": "http://127.0.0.1:8000/mcp",
        "AUTO_BROWSER_BEARER_TOKEN": ""
      }
    }
  }
}
```

## 4. Restart Cursor

Cursor needs a restart to pick up new MCP servers.

## 5. Verify it's connected

Open a new Cursor chat. Type:

```
Use auto-browser to create a session at https://example.com and tell me the page title.
```

If connected, Cursor will call `browser.create_session` and `browser.observe` and return the title.

## If your API is protected

Set `AUTO_BROWSER_BEARER_TOKEN` to match your `API_BEARER_TOKEN` env var:

```json
"AUTO_BROWSER_BEARER_TOKEN": "your-token-here"
```

## Notes

- The stdio bridge proxies Cursor to `http://127.0.0.1:8000/mcp`
- Auto Browser must be running before Cursor tries to use it
- Visual takeover is at `http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale`
- See `examples/claude_desktop_config.json` for the equivalent Claude Desktop config
