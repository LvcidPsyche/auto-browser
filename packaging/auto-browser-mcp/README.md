# auto-browser-mcp

Stdio MCP bridge for [Auto Browser](https://github.com/LvcidPsyche/auto-browser) — give your AI agent a real browser, with a human in the loop.

This is a metapackage: it installs [`auto-browser-client`](https://pypi.org/project/auto-browser-client/) and exposes the `auto-browser-mcp` command so MCP clients can launch the bridge with a single `uvx` invocation.

## Usage

With a local Auto Browser stack running (`docker compose up` in the repo):

```json
{
  "mcpServers": {
    "auto-browser": {
      "command": "uvx",
      "args": ["auto-browser-mcp"]
    }
  }
}
```

Pass `--base-url` / `--bearer-token` (or set `AUTO_BROWSER_BASE_URL` / `AUTO_BROWSER_BEARER_TOKEN`) for non-default deployments.

See the [Auto Browser repository](https://github.com/LvcidPsyche/auto-browser) for the full server, docs, and examples.
