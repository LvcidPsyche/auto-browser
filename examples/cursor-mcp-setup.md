# Cursor MCP setup

## 1. Start Auto Browser

```bash
docker compose up --build
```

## 2. Open Cursor MCP settings

In Cursor: **Settings → MCP** (or open `~/.cursor/mcp.json` directly).

## 3. Add this config block

Cursor speaks MCP's HTTP transport, so it can connect straight to the controller:

```json
{
  "mcpServers": {
    "auto-browser": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer <contents of data/api-token>" }
    }
  }
}
```

On first start the controller saves its API token to `data/api-token` in the
auto-browser folder. If you set `API_BEARER_TOKEN`, use that instead.

To go through the stdio bridge instead, with [uv](https://docs.astral.sh/uv/)
installed (no clone needed):

```json
{
  "mcpServers": {
    "auto-browser": {
      "command": "uvx",
      "args": ["auto-browser-mcp"],
      "env": {
        "AUTO_BROWSER_BASE_URL": "http://127.0.0.1:8000/mcp",
        "AUTO_BROWSER_TOKEN_FILE": "<ABSOLUTE_PATH_TO_AUTO_BROWSER>/data/api-token",
        "AUTO_BROWSER_BEARER_TOKEN": ""
      }
    }
  }
}
```

Replace `<ABSOLUTE_PATH_TO_AUTO_BROWSER>` with the real path to your clone; the bridge reads the
token file on every request.

A bridge older than the one that added `--token-file` ignores `AUTO_BROWSER_TOKEN_FILE`, so every tool call fails with HTTP 401: use `uvx auto-browser-mcp@latest`, or set `AUTO_BROWSER_BEARER_TOKEN` to the file's contents.

Or from a repo checkout, replace `<ABSOLUTE_PATH_TO_AUTO_BROWSER>` with the real path to your clone.
This wrapper reads that checkout's `data/api-token` itself:

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

## If you set API_BEARER_TOKEN

Set `AUTO_BROWSER_BEARER_TOKEN` to match your `API_BEARER_TOKEN` env var (a non-empty one wins
over the token file):

```json
"AUTO_BROWSER_BEARER_TOKEN": "your-token-here"
```

## Notes

- The stdio bridge proxies Cursor to `http://127.0.0.1:8000/mcp`
- Auto Browser must be running before Cursor tries to use it
- Visual takeover is at `http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale`
- See `examples/claude_desktop_config.json` for the equivalent Claude Desktop config
