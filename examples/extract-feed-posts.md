# Extract feed posts

This is the smallest visible-feed extraction flow. There is no feed-specific
tool: read the posts with the generic DOM tools.

Run these from your auto-browser folder. The controller demands its API token, which it
saves to `data/api-token` on first start. Load the header once per shell (if you set
`API_BEARER_TOKEN`, use `$API_BEARER_TOKEN` in place of `$(cat data/api-token)`):

```bash
AUTH=(-H "Authorization: Bearer $(cat data/api-token)")
```

Most feeds wrap each post in an `article` element. `browser.find_elements`
returns one entry per match with its text:

```bash
curl -s "${AUTH[@]}" http://127.0.0.1:8000/mcp/tools/call \
  -X POST \
  -H 'content-type: application/json' \
  -d '{
    "name": "browser.find_elements",
    "arguments": {
      "session_id": "<session-id>",
      "selector": "article",
      "limit": 10
    }
  }' | jq
```

If the feed has no stable post selector, read the page's visible text instead.
`browser.get_html` with `text_only` keeps line breaks, and pages long results
with `offset` and `max_chars`:

```bash
curl -s "${AUTH[@]}" http://127.0.0.1:8000/mcp/tools/call \
  -X POST \
  -H 'content-type: application/json' \
  -d '{
    "name": "browser.get_html",
    "arguments": {
      "session_id": "<session-id>",
      "text_only": true
    }
  }' | jq
```

If you need more posts visible first, use a generic scroll action:

```bash
curl -s "${AUTH[@]}" http://127.0.0.1:8000/mcp/tools/call \
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
