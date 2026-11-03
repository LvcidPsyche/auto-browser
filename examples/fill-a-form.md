# Fill a form

Use `browser.observe` first so you have current `element_id` values.

## Example

Run these from your auto-browser folder. The controller demands its API token, which it
saves to `data/api-token` on first start. Load the header once per shell (if you set
`API_BEARER_TOKEN`, use `$API_BEARER_TOKEN` in place of `$(cat data/api-token)`):

```bash
AUTH=(-H "Authorization: Bearer $(cat data/api-token)")
```

Create a session:

```bash
curl -s "${AUTH[@]}" http://127.0.0.1:8000/sessions \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"name":"form-demo","start_url":"https://example.com"}' | jq
```

Then call the MCP convenience endpoint with a type action:

```bash
curl -s "${AUTH[@]}" http://127.0.0.1:8000/mcp/tools/call \
  -X POST \
  -H 'content-type: application/json' \
  -d '{
    "name": "browser.execute_action",
    "arguments": {
      "session_id": "<session-id>",
      "action": {
        "action": "type",
        "reason": "Fill the email field",
        "element_id": "op-email",
        "text": "test@example.com"
      }
    }
  }' | jq
```
