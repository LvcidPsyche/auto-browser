# Claude Desktop setup

1. Start Auto Browser locally:

```bash
docker compose up --build
```

2. Copy `claude_desktop_config.json` from this folder.
3. Replace `<ABSOLUTE_PATH_TO_AUTO_BROWSER>` with your real clone path. On first start the controller
   saves its API token to `data/api-token` there, and the bridge reads it on every request.
4. If you set `API_BEARER_TOKEN` on the controller, put it in `AUTO_BROWSER_BEARER_TOKEN`
   instead (a non-empty one wins over the file). If it requires an operator id, also set
   `AUTO_BROWSER_OPERATOR_ID`.
5. Paste the config into Claude Desktop and restart it.

A bridge older than the one that added `--token-file` ignores `AUTO_BROWSER_TOKEN_FILE`, so every
tool call fails with HTTP 401. Use `"args": ["auto-browser-mcp@latest"]`, or set
`AUTO_BROWSER_BEARER_TOKEN` to the contents of `data/api-token`.

The stdio bridge will proxy Claude Desktop to:

```text
http://127.0.0.1:8000/mcp
```
