# MCP Client Integration

Auto Browser exposes a real MCP transport at:

```text
http://127.0.0.1:8000/mcp
```

It also exposes convenience endpoints at:

```text
http://127.0.0.1:8000/mcp/tools
http://127.0.0.1:8000/mcp/tools/call
```

## Transport model

Auto Browser exposes:

- an **HTTP MCP server**
- a **stdio bridge**, installable from PyPI (`uvx auto-browser-mcp`) or bundled at `scripts/mcp_stdio_bridge.py` for repo checkouts

That means:
- MCP clients with HTTP transport support can talk to it directly
- stdio-first clients can use the bridge with zero setup beyond `uvx`

## Connect directly over HTTP

Clients that speak MCP's HTTP transport need no bridge: point them at
`http://127.0.0.1:8000/mcp`.

**Claude Code**

```bash
claude mcp add --transport http auto-browser http://127.0.0.1:8000/mcp
claude mcp list   # auto-browser: http://127.0.0.1:8000/mcp (HTTP) - ✓ Connected
```

**Codex**: the CLI, the IDE extension, and the desktop app share
`~/.codex/config.toml`, so one registration covers all three:

```bash
codex mcp add auto-browser --url http://127.0.0.1:8000/mcp
codex mcp list
```

The same entry written by hand in `~/.codex/config.toml`:

```toml
[mcp_servers.auto-browser]
url = "http://127.0.0.1:8000/mcp"
```

Codex renames tools to fit OpenAI's function-name rules, which do not allow the
dots in names like `browser.observe`, and maps the calls back. Nothing to configure.

**Google Antigravity**: open the agent panel's menu, then MCP Servers, Manage MCP
Servers, and View raw config (in Antigravity 2.0: Settings, Customizations,
Installed MCP Servers). That opens `~/.gemini/config/mcp_config.json`; for one
workspace, use `.agents/mcp_config.json` instead:

```json
{
  "mcpServers": {
    "auto-browser": {
      "serverUrl": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

Antigravity reads `serverUrl` and ignores the `url` key the Cursor example below
uses, so a copied Cursor entry never connects.

**Cursor**: `~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project:

```json
{
  "mcpServers": {
    "auto-browser": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

**VS Code**: `.vscode/mcp.json` in the workspace:

```json
{
  "servers": {
    "auto-browser": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

If the controller has an `API_BEARER_TOKEN`, send it as a header. With Claude
Code, add `--header "Authorization: Bearer $API_BEARER_TOKEN"`. Codex reads the
token from an environment variable you name: export it, then add
`--bearer-token-env-var API_BEARER_TOKEN` to `codex mcp add` (in
`config.toml`, `bearer_token_env_var = "API_BEARER_TOKEN"`). With Antigravity,
Cursor, and VS Code, add `"headers": {"Authorization": "Bearer <token>"}` next
to the URL. Without the header, the client reports a 401 "Missing or invalid
bearer token". With `REQUIRE_OPERATOR_ID=true`, also send `X-Operator-Id` (or
whatever `OPERATOR_ID_HEADER` names) the same way; in Codex that is
`http_headers = { "X-Operator-Id" = "<your id>" }` in `config.toml`.

Claude Desktop's config file only launches local (stdio) servers, so it uses
the bridge below.

## Why this matters

Most browser automation projects are just scripts or raw APIs.

Auto Browser is interesting because it already packages the browser layer as an MCP-native tool server with:
- session lifecycle
- observations
- tool calls
- approvals
- auth profile reuse
- human takeover

## Recommended local setup

1. start Auto Browser locally
2. confirm `make doctor` passes
3. point your MCP-capable client at `/mcp`
4. use the browser tools for:
   - session create
   - observe
   - actions
   - auth profile save/reuse

## Claude Desktop example

Auto Browser ships a copy-paste Claude Desktop config example:

- `examples/claude_desktop_config.json`
- `examples/claude-desktop-setup.md`

Minimal shape (requires [uv](https://docs.astral.sh/uv/)):

```json
{
  "mcpServers": {
    "auto-browser": {
      "command": "uvx",
      "args": ["auto-browser-mcp"],
      "env": {
        "AUTO_BROWSER_BASE_URL": "http://127.0.0.1:8000/mcp",
        "AUTO_BROWSER_BEARER_TOKEN": ""
      }
    }
  }
}
```

Working from a repo checkout without uv? Point `command` at `python3` with
`args: ["/ABSOLUTE/PATH/TO/auto-browser/scripts/mcp_stdio_bridge.py"]` instead.

If your API is protected, set `AUTO_BROWSER_BEARER_TOKEN`. If it sets
`REQUIRE_OPERATOR_ID=true`, also set `AUTO_BROWSER_OPERATOR_ID`, unless the token is a
named credential from `API_BEARER_TOKENS`.

## Pairing with other MCP servers

Auto Browser drives a browser. It does not search the web, and it does not need
to: MCP clients run several servers side by side, and the model picks tools
from all of them. To have an agent research something, add a web-search MCP
server next to Auto Browser in the same client, following that server's own
setup instructions. The agent then searches with one and opens and works the
results with the other.

With Claude Code, for example:

```bash
claude mcp add --transport http auto-browser http://127.0.0.1:8000/mcp
claude mcp add ...   # your search server, as its docs describe
```

Or in a JSON client config, one entry per server:

```json
{
  "mcpServers": {
    "auto-browser": { "url": "http://127.0.0.1:8000/mcp" },
    "search": { "...": "your search server's entry" }
  }
}
```

Search results point anywhere, and navigation is limited to `ALLOWED_HOSTS`,
which defaults to `example.com` and localhost. List the domains the agent may
open, or set `ALLOWED_HOSTS=*` on a deployment where open browsing is
acceptable. A refused navigation names the host that was blocked. The list governs the
top-level page: a page that moves itself to another host is sent back to `about:blank`,
but iframes and subresources loaded from other hosts are not filtered.

Keeping search as a separate server, not a tool inside Auto Browser, lets you
choose the search provider and its API key independently, and keeps Auto
Browser's tool list, which every request carries, short.

## Recommended first demo

The best MCP demo is:

1. create session
2. navigate/login manually once if needed
3. save auth profile
4. open a second session from that auth profile
5. continue work through MCP tools

That shows why MCP + browser state reuse is more valuable than a plain “open page and click things” demo.

## Resources and subscriptions

MCP clients can inspect browser state through resources as well as one-shot
tools. Start with a normal MCP initialization, keep the returned
`MCP-Session-Id` and `MCP-Protocol-Version` headers, then send
`notifications/initialized`.

Minimal resource listing over the HTTP transport:

```bash
curl -s http://127.0.0.1:8000/mcp \
  -X POST \
  -H 'accept: application/json, text/event-stream' \
  -H 'content-type: application/json' \
  -H "MCP-Session-Id: $MCP_SESSION_ID" \
  -H "MCP-Protocol-Version: $MCP_PROTOCOL_VERSION" \
  -d '{
    "jsonrpc": "2.0",
    "id": 20,
    "method": "resources/list",
    "params": {}
  }' | jq
```

The default resource set includes:

- `browser://sessions` for active browser sessions
- `browser://audit/events` for recent audit events
- `browser://<session-id>/dom`, `browser://<session-id>/screenshot`,
  `browser://<session-id>/console`, and `browser://<session-id>/network` once a
  session exists

Read a resource by URI:

```bash
curl -s http://127.0.0.1:8000/mcp \
  -X POST \
  -H 'accept: application/json, text/event-stream' \
  -H 'content-type: application/json' \
  -H "MCP-Session-Id: $MCP_SESSION_ID" \
  -H "MCP-Protocol-Version: $MCP_PROTOCOL_VERSION" \
  -d '{
    "jsonrpc": "2.0",
    "id": 21,
    "method": "resources/read",
    "params": {
      "uri": "browser://audit/events"
    }
  }' | jq
```

Clients that support resource updates can subscribe, then keep the MCP event
stream open with `GET /mcp`:

```bash
curl -s http://127.0.0.1:8000/mcp \
  -X POST \
  -H 'accept: application/json, text/event-stream' \
  -H 'content-type: application/json' \
  -H "MCP-Session-Id: $MCP_SESSION_ID" \
  -H "MCP-Protocol-Version: $MCP_PROTOCOL_VERSION" \
  -d '{
    "jsonrpc": "2.0",
    "id": 22,
    "method": "resources/subscribe",
    "params": {
      "uri": "browser://sessions"
    }
  }' | jq

curl -N http://127.0.0.1:8000/mcp \
  -H 'accept: text/event-stream' \
  -H "MCP-Session-Id: $MCP_SESSION_ID" \
  -H "MCP-Protocol-Version: $MCP_PROTOCOL_VERSION"
```

When a subscribed resource changes, the stream emits a JSON-RPC notification
named `notifications/resources/updated` with the updated `uri`.

## MCP tool profiles and packs

Every tool the controller lists costs the model context on every request, so the
controller serves a selection fixed at startup. `MCP_TOOL_PROFILE` picks a profile;
`MCP_TOOL_PACKS` adds packs by name or removes them with `-name`.

| Profile | Packs | Tools | `tools/list` size | For |
|---|---|---|---|---|
| `lean` | `browse` | 7 | about 12 KB | the browsing loop: observe, act, read the page, hand to a person, close |
| `curated` (default) | `browse`, `sessions`, `tabs`, `downloads`, `auth_profiles`, `wait`, `scripting`, `vision` | 20 (21 with a vision model) | about 25 KB | a browsing agent: adds sessions with options, saved sign-ins, tabs, downloads, waits and `eval_js` |
| `full` | every pack | 74 (75 with a vision model) | about 68 KB | everything: diagnostics, Witness, admin, the agent queue, the harness |

Sizes are the JSON `tools/list` returns, without a vision model.

Every tool is in exactly one pack, the unit you switch on or off. Each row lists its
tools in the order `tools/list` returns them.

| Pack | Smallest profile | For | Tools |
|---|---|---|---|
| `browse` | lean | observe, act, read the page, hand to a person, close | `browser.observe`, `browser.screenshot`, `browser.execute_action`, `browser.request_human_takeover`, `browser.close_session`, `browser.get_html`, `browser.find_elements` |
| `sessions` | curated | opening a session with options (start URL, saved sign-in, proxy, memory profile); listing, inspecting and forking sessions | `browser.create_session`, `browser.list_sessions`, `browser.get_session`, `browser.fork_session` |
| `tabs` | curated | popups and multi-tab flows | `browser.list_tabs`, `browser.activate_tab`, `browser.close_tab` |
| `downloads` | curated | reading files the page downloads | `browser.list_downloads`, `browser.read_download` |
| `auth_profiles` | curated | listing saved sign-ins, and saving a session's sign-in to disk | `browser.list_auth_profiles`, `browser.save_auth_profile` |
| `wait` | curated | waiting for an element (the `wait` action waits a fixed time) | `browser.wait_for_selector` |
| `scripting` | curated | JavaScript in the page; every call needs an operator's approval | `browser.eval_js` |
| `vision` | curated | finding an element from a description; listed only with a vision model (`ANTHROPIC_API_KEY`) | `browser.find_by_vision` |
| `devtools` | full | debugging a page: console, page errors and failed requests beyond observe's last ten, the network log, traces, script export | `browser.get_console`, `browser.get_page_errors`, `browser.get_request_failures`, `browser.stop_trace`, `browser.get_network_log`, `browser.export_script` |
| `deployment` | full | the controller's own setup: remote access, the readiness check, the PII scrubber | `browser.get_remote_access`, `browser.readiness_check`, `browser.pii_scrubber_status` |
| `witness` | full | verifying and exporting a session's Witness receipts | `browser.verify_witness`, `browser.export_witness_bundle` |
| `memory_profiles` | full | memory profiles that new sessions load | `browser.save_memory_profile`, `browser.get_memory_profile`, `browser.list_memory_profiles`, `browser.delete_memory_profile` |
| `advanced_actions` | full | drag and drop, viewport size | `browser.drag_drop`, `browser.set_viewport` |
| `storage` | full | cookies, and local and session storage | `browser.get_cookies`, `browser.set_cookies`, `browser.get_local_storage`, `browser.set_local_storage` |
| `auth_admin` | full | inspecting a saved sign-in's files, saving a session's raw storage state | `browser.get_auth_profile`, `browser.save_auth_state` |
| `session_admin` | full | attaching to a running Chrome, sharing a session, switching a session to a visible window | `browser.cdp_attach`, `browser.share_session`, `browser.enable_shadow_browse` |
| `approvals` | full | listing and deciding approvals over MCP | `browser.list_approvals`, `browser.approve_approval`, `browser.reject_approval`, `browser.execute_approval` |
| `agent_jobs` | full | the built-in agent queue and its model providers | `browser.list_agent_jobs`, `browser.get_agent_job`, `browser.resume_agent_job`, `browser.discard_agent_job`, `browser.cancel_agent_job`, `browser.queue_agent_step`, `browser.queue_agent_run`, `browser.list_providers` |
| `proxies` | full | proxy personas | `browser.list_proxy_personas`, `browser.create_proxy_persona`, `browser.delete_proxy_persona` |
| `cron` | full | agent runs on a schedule or a webhook | `browser.list_cron_jobs`, `browser.create_cron_job`, `browser.delete_cron_job`, `browser.trigger_cron_job` |
| `harness` | full | the convergence harness | `harness.start_convergence`, `harness.get_status`, `harness.get_trace`, `harness.list_runs`, `harness.list_candidates`, `harness.get_candidate`, `harness.check_drift`, `harness.check_all_drifts`, `harness.graduate` |

```bash
MCP_TOOL_PROFILE=lean                                  # 7 tools, 11,934 B
MCP_TOOL_PROFILE=lean MCP_TOOL_PACKS=sessions,tabs     # 14 tools
MCP_TOOL_PACKS=harness                                 # curated + harness: 29 tools, 34,077 B
MCP_TOOL_PACKS=-scripting                              # curated without eval_js: 19 tools, 23,913 B
MCP_TOOL_PROFILE=full MCP_TOOL_PACKS=-harness,-cron    # 61 tools, 57,117 B
```

The rules:
- `MCP_TOOL_PACKS` is a comma-separated list on top of the profile: a name adds a pack and
  `-name` removes one. Removals apply after additions, whatever order they are written in, and
  removing a pack the selection does not have changes nothing.
- Names are exact and lowercase. `MCP_TOOL_PROFILE` unset, empty or blank means `curated`.
- An unknown pack in `MCP_TOOL_PACKS` stops the controller at startup, with the valid names in
  the error. An unknown `MCP_TOOL_PROFILE` (`leen`, `FULL`) serves `curated` and logs a warning
  that names the closest profile when one is close; a typo used to give `curated` without a word.
- The startup log says what is served, for example
  `MCP tools: lean profile with MCP_TOOL_PACKS=tabs, 10 tools listed`. `GET /mcp/tools` lists
  the tools themselves.
- Both settings are read once, at startup. After changing one, restart the controller; MCP
  clients pick up the new list when they reconnect (the controller does not send
  `notifications/tools/list_changed`).

Packs choose what MCP clients see; they are not access control: the REST API keeps its routes
for callers with the token, and agent self-approval still needs `AUTONOMOUS_APPROVALS=true`.

What the model sees follows the selection: `tools/list`, and the instructions sent on
`initialize`, which leave out any sentence naming a tool the controller does not serve. A call
to a tool outside the selection returns an error naming its pack, not "unknown tool":

```text
browser.get_console is in the devtools tool pack, which this controller does not serve (it serves the curated profile). To use it, add devtools to MCP_TOOL_PACKS or set MCP_TOOL_PROFILE=full on the controller.
```

`browser.queue_agent_run` (pack `agent_jobs`) accepts `workflow_profile`
(`fast` or `governed`) and `browser.resume_agent_job` resumes interrupted,
failed, or step-limited background runs from persisted checkpoints.

Use `workflow_profile=governed` when starting convergence against a live browser session. `harness.graduate` writes only a staged candidate; promotion to the production skill corpus remains a separate governed review step.

### Before choosing lean

- **Opening sessions.** `browser.observe`, `browser.execute_action`, `browser.screenshot`,
  `browser.get_html` and `browser.find_elements` open a session with the deployment's defaults
  when none is live, so lean needs no `browser.create_session`; a start URL is an
  `execute_action` navigate. Add `sessions` to open one with a saved sign-in, proxy persona or
  memory profile, or to list, inspect and fork sessions.
- **Session ids.** On every profile the `session_id` argument's description names
  `browser.create_session` and `browser.list_sessions`, and `detail`'s names
  `browser.get_session`. On lean, omit `session_id` while one session is live, or take
  `session.id` from an observe result.
- **New tabs and popups.** A tab the page opens is listed in observations but not switched
  to; add `tabs` for tasks that work in one.
- **Downloads, waits and scripts.** Add `downloads` to read downloaded files, `wait` for
  `browser.wait_for_selector`, and `scripting` for `browser.eval_js`.
- **LangChain.** The `AutoBrowserNode` in `integrations/langchain` opens its sessions with
  `browser.create_session`, so it needs `sessions` on a lean controller.

`lean` makes each request cheaper. Whether it also improves tool choice has not been measured.

### Governed approvals on tool calls

Pass `workflow_profile=governed` on a tool call (it defaults to `fast`) and every tool that
writes asks for an operator's approval first: the call returns `approval_required` with an
`approval_id`, an operator approves it, and the agent repeats the same call with that
`approval_id`. An approval covers exactly one call with exactly those arguments.

A governed tool that names no session (`browser.delete_memory_profile`, `browser.cdp_attach`)
is approved in a controller-wide scope: the approval's `session_id` is `controller`, and
`GET /approvals?session_id=controller` lists the ones you may see. With no session there is no
session owner, so under named credentials (`API_BEARER_TOKENS`) such an approval belongs to
the operator who asked and to nobody else: another operator cannot list, decide, reuse or
spend it. `POST /approvals/{id}/execute` does not run it (the agent repeats the tool call).

`workflow_profile` is the caller's argument, so by default governed approvals are the agent's
choice. A deployment that wants them as a rule sets `FORCE_GOVERNED_APPROVALS=true`: every tool
with a governed kind then runs as `governed` whatever the call passed, with or without a
session, and `fast` is no longer available for them. Tools without a governed kind (reads)
are unchanged. It also forces every step of the agent loop to `governed`, whatever
`request.workflow_profile` says, so `browser.queue_agent_step` and `browser.queue_agent_run`, REST
`/agent/step` and `/agent/run`, and cron runs cannot be used to get around it.

It does not cover the REST action routes (`/sessions/{id}/actions/*`): they ask for approval
only for uploads and for `post`, `payment`, `account_change` and `destructive` decisions, and an
agent holding a bearer token can call them directly. The setting is a control on agents that
act through MCP tools and the agent loop; it is not a sandbox for a token holder. It is off by
default because an agent loop written for `fast` stalls on its first write until an operator
approves. With `AUTONOMOUS_APPROVALS=true` the agent can approve its own call, which cancels
the effect. See `docs/production-hardening.md`.

## Raw tool-call example

If you want to see the shape of the tool surface without wiring up a full MCP client yet:

```bash
curl -s http://127.0.0.1:8000/mcp/tools | jq

curl -s http://127.0.0.1:8000/mcp/tools/call \
  -X POST \
  -H 'content-type: application/json' \
  -d '{
    "name": "browser.create_session",
    "arguments": {
      "name": "demo",
      "start_url": "https://example.com"
    }
  }' | jq
```
