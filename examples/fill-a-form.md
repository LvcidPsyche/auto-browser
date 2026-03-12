# Fill a form

Use `browser.observe` first so you have current `element_id` values.

## Example

Create a session:

```bash
curl -s http://127.0.0.1:8000/sessions \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"name":"form-demo","start_url":"https://example.com"}' | jq
```

Then call the MCP convenience endpoint with a type action:

```bash
curl -s http://127.0.0.1:8000/mcp/tools/call \
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
