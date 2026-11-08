# auto-browser-client

Python SDK and MCP stdio bridge for the [Auto Browser](https://github.com/LvcidPsyche/auto-browser) REST API.

## SDK

```python
from auto_browser_client import AutoBrowserClient

client = AutoBrowserClient("http://localhost:8000", token_file="data/api-token")
session = client.create_session(start_url="https://example.com")
client.navigate(session["id"], "https://example.com/dashboard")
client.close_session(session["id"])
```

A local controller with no `API_BEARER_TOKEN` saves its generated token to
`data/api-token` in the auto-browser folder; `token_file` reads it once, when the
client is built (a relative path is relative to where your code runs). Pass
`token=` instead for a token you set. A missing file raises `FileNotFoundError`, an
empty one `ValueError`.

Every method has an `async_` twin (`await client.async_create_session(...)`), and the
client works as a sync or async context manager. HTTP errors raise
`AutoBrowserError`, which carries `status_code` and the controller's `detail`.

An `AutoBrowserError`, and httpx's own errors raised from a call (a connection or
protocol failure), leave out the token and the base URL's userinfo, query and
fragment. httpx itself logs every request at INFO on its `httpx` logger, naming the
URL as sent (userinfo, query and fragment included) and the server's reason phrase,
and this package does not filter that logger: keep a secret in `token` or
`token_file`, not in the base URL. A base URL holding a user name or password is
refused with a token (`ValueError`), since httpx would send them as Basic credentials
in place of it; without a token they go as Basic credentials. A base URL's query is
sent with every request, as httpx writes parameters; its fragment is never sent.

A controller with `REQUIRE_OPERATOR_ID=true` rejects requests that don't name an
operator. A named credential (`API_BEARER_TOKENS`) names one through the token;
with the shared token, pass `operator_id`. Any other headers go in `headers`:

```python
from auto_browser_client import AutoBrowserClient, AutoBrowserError

client = AutoBrowserClient(
    "http://localhost:8000",
    token="secret",
    operator_id="alice",
)
try:
    client.observe(session_id, preset="text")
except AutoBrowserError as exc:
    print(exc.status_code, exc.detail)
```

## MCP stdio bridge

This package also ships the `auto-browser-mcp` console script, which bridges
stdio MCP clients (Claude Desktop, Cursor, ...) to a running Auto Browser
controller:

```bash
auto-browser-mcp --base-url http://127.0.0.1:8000/mcp \
  --token-file /ABSOLUTE/PATH/TO/auto-browser/data/api-token
```

`--token-file` (or `AUTO_BROWSER_TOKEN_FILE`) is read on every request, so the bridge
can start before the controller has written it. `--bearer-token` (or
`AUTO_BROWSER_BEARER_TOKEN`) wins when it is set.

A bridge older than the one that added `--token-file` ignores `AUTO_BROWSER_TOKEN_FILE`, so every tool call fails with HTTP 401: use `uvx auto-browser-mcp@latest`, or set `AUTO_BROWSER_BEARER_TOKEN` to the file's contents.

Or zero-install via [uv](https://docs.astral.sh/uv/): `uvx auto-browser-mcp`
(resolved through the [auto-browser-mcp](https://pypi.org/project/auto-browser-mcp/)
metapackage).

The server itself is Docker-first — see the
[Auto Browser repository](https://github.com/LvcidPsyche/auto-browser) to run it.
