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
      "args": ["auto-browser-mcp"],
      "env": {
        "AUTO_BROWSER_TOKEN_FILE": "/ABSOLUTE/PATH/TO/auto-browser/data/api-token"
      }
    }
  }
}
```

On first start the controller saves its API token to `data/api-token` in the auto-browser folder, and the bridge reads it from `--token-file` / `AUTO_BROWSER_TOKEN_FILE` on every request, so it can start before the controller. Pass `--base-url` / `--bearer-token` / `--operator-id` (or set `AUTO_BROWSER_BASE_URL` / `AUTO_BROWSER_BEARER_TOKEN` / `AUTO_BROWSER_OPERATOR_ID`) for non-default deployments; a bearer token wins over the file.

A bridge older than the one that added `--token-file` ignores `AUTO_BROWSER_TOKEN_FILE`, so every tool call fails with HTTP 401: use `uvx auto-browser-mcp@latest`, or set `AUTO_BROWSER_BEARER_TOKEN` to the file's contents.

See the [Auto Browser repository](https://github.com/LvcidPsyche/auto-browser) for the full server, docs, and examples.
