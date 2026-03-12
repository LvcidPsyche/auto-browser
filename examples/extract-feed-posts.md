# Extract feed posts

This is the smallest visible-feed extraction flow.

```bash
curl -s http://127.0.0.1:8000/mcp/tools/call \
  -X POST \
  -H 'content-type: application/json' \
  -d '{
    "name": "social.extract_posts",
    "arguments": {
      "session_id": "<session-id>",
      "limit": 10
    }
  }' | jq
```

If you need more posts visible first, use a generic scroll action:

```bash
curl -s http://127.0.0.1:8000/mcp/tools/call \
  -X POST \
  -H 'content-type: application/json' \
  -d '{
    "name": "browser.execute_action",
    "arguments": {
      "session_id": "<session-id>",
      "action": {
        "action": "scroll",
        "reason": "Load more feed items",
        "delta_y": 1200
      }
    }
  }' | jq
```
