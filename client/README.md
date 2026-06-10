# auto-browser-client

Python SDK and MCP stdio bridge for the [Auto Browser](https://github.com/LvcidPsyche/auto-browser) REST API.

## SDK

```python
from auto_browser_client import AutoBrowserClient

client = AutoBrowserClient("http://localhost:8000", token="secret")
session = client.create_session(start_url="https://example.com")
client.navigate(session["id"], "https://example.com/dashboard")
client.close_session(session["id"])
```

## MCP stdio bridge

This package also ships the `auto-browser-mcp` console script, which bridges
stdio MCP clients (Claude Desktop, Cursor, ...) to a running Auto Browser
controller:

```bash
auto-browser-mcp --base-url http://127.0.0.1:8000/mcp
```

Or zero-install via [uv](https://docs.astral.sh/uv/): `uvx auto-browser-mcp`
(resolved through the [auto-browser-mcp](https://pypi.org/project/auto-browser-mcp/)
metapackage).

The server itself is Docker-first — see the
[Auto Browser repository](https://github.com/LvcidPsyche/auto-browser) to run it.
