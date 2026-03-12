# Examples

These are small curl-first examples for common flows.

More walkthroughs:

- `claude-desktop-setup.md`
- `login-and-save-profile.md`
- `fill-a-form.md`
- `extract-feed-posts.md`
- `claude_desktop_config.json`

## 1. Create a session

```bash
curl -s http://127.0.0.1:8000/sessions \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"name":"demo","start_url":"https://example.com"}' | jq
```

## 2. Observe the page

```bash
curl -s http://127.0.0.1:8000/sessions/<session-id>/observe | jq
```

## 3. Capture a lightweight screenshot

```bash
curl -s http://127.0.0.1:8000/sessions/<session-id>/screenshot \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"label":"checkpoint"}' | jq
```

## 4. Save a reusable auth profile

```bash
curl -s http://127.0.0.1:8000/sessions/<session-id>/auth-profiles \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"profile_name":"outlook-default"}' | jq
```

## 5. List auth profiles

```bash
curl -s http://127.0.0.1:8000/auth-profiles | jq
```

## 6. Resume a new session from a saved profile

```bash
curl -s http://127.0.0.1:8000/sessions \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"name":"mail","start_url":"https://outlook.live.com/mail/0/","auth_profile":"outlook-default"}' | jq
```

## 7. Type a secret safely

```bash
curl -s http://127.0.0.1:8000/sessions/<session-id>/actions/type \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"element_id":"op-password","text":"secret","sensitive":true}' | jq
```

## 8. Ask for human takeover

```bash
curl -s http://127.0.0.1:8000/sessions/<session-id>/takeover \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"reason":"Manual checkpoint"}' | jq
```

## 9. List MCP tools

```bash
curl -s http://127.0.0.1:8000/mcp/tools | jq
```

## 10. Call one MCP tool through the convenience endpoint

```bash
curl -s http://127.0.0.1:8000/mcp/tools/call \
  -X POST \
  -H 'content-type: application/json' \
  -d '{
    "name": "browser.list_sessions",
    "arguments": {}
  }' | jq
```
