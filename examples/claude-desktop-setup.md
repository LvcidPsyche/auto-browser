# Claude Desktop setup

1. Start Auto Browser locally:

```bash
docker compose up --build
```

2. Copy `claude_desktop_config.json` from this folder.
3. Replace `<ABSOLUTE_PATH_TO_AUTO_BROWSER>` with your real clone path.
4. If your API is protected, set `AUTO_BROWSER_BEARER_TOKEN`.
5. Paste the config into Claude Desktop and restart it.

The stdio bridge will proxy Claude Desktop to:

```text
http://127.0.0.1:8000/mcp
```
