# Sitemap Memory

**Sitemap Memory** lets browser agents remember what they learn about websites. Instead of rediscovering page layouts, selectors, and APIs on every visit, the agent builds up knowledge that persists across sessions.

**Result:** Up to 90% fewer tokens on repeat visits.

---

## How It Works

### What Gets Stored

| Type | Example | Use |
|------|---------|-----|
| **Page Selectors** | `{"login_btn": "#submit", "username": "#email"}` | Click/fill without re-finding |
| **API Endpoints** | `GET /api/v1/users` | Call APIs directly |
| **Navigation Paths** | Login flow: 5 steps | Replay multi-step workflows |
| **Notes** | "Rate limited after 100 requests" | Agent reminders |
| **Sensitive URLs** | `/settings/delete-account` | Trigger human approval |

### What's NOT Stored

- Passwords, tokens, API keys
- Cookies or session data
- JWTs or bearer tokens
- Any credential-like content

The system automatically rejects secret-bearing text.

---

## Using Sitemap Memory

### As an Agent

When you browse a site, memory is automatically loaded and injected into your context:

```
[Site Memory: github.com]

Known pages:
  /login: {'submit_btn': 'button[type="submit"]', 'username': '#login_field'}
  /*/issues: {'new_issue': 'a[href$="/issues/new"]'}

API endpoints:
  search: GET /search?q={query}
  user_repos: GET /users/{user}/repos

Navigation paths:
  login_flow: 3 steps (/ → /login)
```

### MCP Tools

| Tool | Purpose |
|------|---------|
| `browser.sitemap_context` | Load memory for a URL |
| `browser.sitemap_show` | View all memory for a site |
| `browser.sitemap_list` | List sites with stored memory |
| `browser.sitemap_note` | Add a note |
| `browser.sitemap_selector` | Record page selectors |
| `browser.sitemap_endpoint` | Record an API endpoint |
| `browser.sitemap_observe` | Record observation for review |
| `browser.sitemap_delete` | Delete all memory for a site |

### Example: Recording a Selector

```json
{
  "tool": "browser.sitemap_selector",
  "input": {
    "url": "https://github.com/login",
    "selectors": {
      "username": "#login_field",
      "password": "#password",
      "submit": "input[type='submit']"
    }
  }
}
```

### Example: Recording an API

```json
{
  "tool": "browser.sitemap_endpoint",
  "input": {
    "url": "https://api.github.com/users/octocat/repos",
    "name": "user_repos",
    "method": "GET",
    "response_path": "$[*]",
    "auth_required": true
  }
}
```

---

## Storage Location

Memory is stored as JSON files:

```
~/.auto-browser/sites/
├── github.com/
│   ├── manifest.json      # Core metadata
│   ├── pages.json         # Page selectors
│   ├── endpoints.json     # API endpoints
│   ├── navigation.json    # Multi-step flows
│   ├── notes.json         # Freeform notes
│   └── candidates/        # Pending observations
└── example.com/
    └── ...
```

Files are human-readable JSON. You can edit them directly if needed.

---

## Observations (Candidates)

When an agent discovers something useful, it can record an **observation**:

```json
{
  "tool": "browser.sitemap_observe",
  "input": {
    "hostname": "github.com",
    "kind": "access",
    "claim": "Create Issue requires login",
    "evidence": "Clicked 'New Issue', redirected to /login",
    "consequence": "Must authenticate before creating issues"
  }
}
```

Observations are stored as **candidates** for human review:

- `pending` — Awaiting review
- `accepted` — Integrated into memory
- `rejected` — Discarded (with reason)

### Observation Types

| Kind | When to Use |
|------|-------------|
| `action_space` | Discovered what actions are available |
| `better_path` | Found a shorter route to a goal |
| `access` | Learned auth requirements |
| `high_consequence` | Found a destructive/sensitive action |
| `repeated_mistake` | Agent made the same error twice |

---

## Governance

### Read Operations
- `sitemap_context`, `sitemap_show`, `sitemap_list`
- No approval required

### Write Operations
- `sitemap_note`, `sitemap_selector`, `sitemap_endpoint`, `sitemap_observe`
- Require approval in governed mode

### Destructive Operations
- `sitemap_delete`
- Always require explicit `confirm: true`

---

## Best Practices

1. **Record selectors after verifying they work** — Don't store guesses
2. **Use descriptive names** — `submit_btn` not `btn1`
3. **Mark endpoints as stale when they change** — Better than wrong info
4. **Use observations for uncertain findings** — Let humans review
5. **Add notes for rate limits, quirks, workarounds** — Future agents benefit

---

## Privacy & Security

- **No credentials stored** — Automatic rejection of passwords, tokens, secrets
- **No cookies** — Session state stays in browser profiles, not sitemap
- **Local storage** — Memory stays on your machine
- **Human review** — Observations require approval before integration
- **Audit trail** — All changes logged

---

## FAQ

**Q: Does memory sync across machines?**
A: Not yet. Memory is stored locally. Cloud sync is planned for a future release.

**Q: What if a site changes?**
A: Mark endpoints as stale with `sitemap_stale`. The agent will re-verify before using cached info.

**Q: Can I share memory between team members?**
A: Yes — copy the `~/.auto-browser/sites/{hostname}/` directory. It's just JSON files.

**Q: How much token savings should I expect?**
A: Typical savings are 50-90% on repeat visits, depending on site complexity.
