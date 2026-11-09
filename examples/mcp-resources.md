# MCP resources & subscriptions

Auto Browser's MCP endpoint exposes live browser state as **resources** and lets
a client **subscribe** to change notifications. This walkthrough shows the raw
JSON-RPC for each — pair it with the tool examples in
[`claude-desktop-setup.md`](./claude-desktop-setup.md).

All calls go to the MCP transport (default `POST /mcp`) with an
`MCP-Session-Id` header once a session is established, and every call sends the
controller's token as `Authorization: Bearer <token>`: the contents of
`data/api-token` in the auto-browser folder, or `API_BEARER_TOKEN` if you set one.

## Resource catalog

`resources/list` returns the resources you can read. `browser://sessions` is
always present; the per-session resources appear once a browser session exists.

```jsonc
// --> request
{ "jsonrpc": "2.0", "id": 1, "method": "resources/list" }

// <-- result
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "resources": [
      { "uri": "browser://sessions",            "name": "Active Sessions", "mimeType": "application/json" },
      { "uri": "browser://<session-id>/screenshot", "name": "Screenshot […]", "mimeType": "image/png" },
      { "uri": "browser://<session-id>/dom",     "name": "DOM […]",        "mimeType": "text/html" },
      { "uri": "browser://<session-id>/console", "name": "Console […]",    "mimeType": "application/json" },
      { "uri": "browser://<session-id>/network", "name": "Network Log […]","mimeType": "application/json" }
    ]
  }
}
```

| URI | mimeType | Content |
|-----|----------|---------|
| `browser://sessions` | `application/json` | All active sessions |
| `browser://audit/events` | `application/json` | Recent audit events (never notified: poll it) |
| `browser://<id>/screenshot` | `image/png` | Latest screenshot for the session |
| `browser://<id>/dom` | `text/html` | Current page HTML |
| `browser://<id>/console` | `application/json` | Recent console messages |
| `browser://<id>/network` | `application/json` | Recent network requests/responses |

## Reading a resource

```jsonc
// --> request
{ "jsonrpc": "2.0", "id": 2, "method": "resources/read",
  "params": { "uri": "browser://sessions" } }

// <-- result: { "contents": [ { "uri": "...", "mimeType": "application/json", "text": "[…]" } ] }
```

## Subscribing to changes

Subscribe to a resource URI to receive a `notifications/resources/updated`
message when a browser event notifies it, for a session you may use. These
events do, and nothing else does:

| Event | Notifies |
|-------|----------|
| Every event below | `browser://sessions` |
| `observe`: the session was observed | `browser://<id>/dom`, `browser://<id>/screenshot` |
| `action`: an action on the page succeeded | `browser://<id>/dom`, `browser://<id>/network`, `browser://<id>/screenshot` |
| `session`: the session closed | `browser://<id>/dom` |
| `approval`: an approval was requested | `browser://<id>/console` |

A notification says only that the URI may have changed, so `resources/read` it
for the new content. Between these events nothing is sent: a console message or
a network request the page makes by itself notifies nobody until one of them
follows. Subscribing to an unknown URI returns error `-32002`; a missing/empty
URI returns `-32602`. `browser://audit/events` is never notified, so subscribing
to it returns `-32602` as well: poll it with `resources/read`.

```jsonc
// --> subscribe
{ "jsonrpc": "2.0", "id": 3, "method": "resources/subscribe",
  "params": { "uri": "browser://<session-id>/dom" } }

// <-- result: {}   (subscription recorded)

// <-- later, pushed by the server after an observe, action or session event:
{ "jsonrpc": "2.0", "method": "notifications/resources/updated",
  "params": { "uri": "browser://<session-id>/dom" } }
```

Unsubscribe when you're done:

```jsonc
{ "jsonrpc": "2.0", "id": 4, "method": "resources/unsubscribe",
  "params": { "uri": "browser://<session-id>/dom" } }
```

Subscriptions are tracked per MCP session and persist across reconnects for the
lifetime of that session. Closing the MCP session drops its subscriptions.

## Typical flow

1. Create a browser session (via the `browser.create_session` tool).
2. `resources/list` to discover the per-session URIs.
3. `resources/subscribe` to `.../dom` and/or `.../network`.
4. Drive the page with tools; react to `notifications/resources/updated`.
5. `resources/read` the updated URI to pull the new content.
