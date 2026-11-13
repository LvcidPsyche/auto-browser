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
- MCP clients with HTTP transport support can talk to it directly, sending the controller's token
- stdio-first clients can use the bridge, which reads the file named by `AUTO_BROWSER_TOKEN_FILE` / `--token-file`; the repo wrapper (`make stdio-bridge`, or `scripts/mcp_stdio_bridge.py`) defaults that to its checkout's `data/api-token` for a loopback URL

The controller keeps the `clientInfo` and the `capabilities` of an `initialize`
with the MCP session it makes. Each may be nested at most 32 levels (the object
itself is the first, and a list is a level as an object is) and take at most
64 KiB written compactly as JSON in UTF-8. An `initialize` past either bound is
answered with JSON-RPC error `-32602`, whose message names the field and the
bound, and makes no session.

## Connect directly over HTTP

Clients that speak MCP's HTTP transport need no bridge: point them at
`http://127.0.0.1:8000/mcp` and send the controller's token. A controller with no
`API_BEARER_TOKEN` generates one on first start and saves it to `data/api-token` in
the auto-browser folder; the commands below run from that folder. If you set
`API_BEARER_TOKEN`, send that instead of the file's contents.

**Claude Code**

```bash
claude mcp add --transport http --scope user auto-browser http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $(cat data/api-token)"
claude mcp list   # auto-browser: http://127.0.0.1:8000/mcp (HTTP) - ✓ Connected
```

`--scope user` makes the server available in every project, not only the auto-browser
folder. Claude Code stores the header when you add the server, so if `data/api-token`
changes, remove the server and add it again.

**Codex**: the CLI, the IDE extension, and the desktop app share
`~/.codex/config.toml`, so one registration covers all three:

```bash
export AUTO_BROWSER_TOKEN="$(cat data/api-token)"
codex mcp add auto-browser --url http://127.0.0.1:8000/mcp --bearer-token-env-var AUTO_BROWSER_TOKEN
codex mcp list
```

Codex reads the token from the variable you name, so export it wherever Codex
runs (a shell profile, for one). The same entry written by hand in
`~/.codex/config.toml`:

```toml
[mcp_servers.auto-browser]
url = "http://127.0.0.1:8000/mcp"
bearer_token_env_var = "AUTO_BROWSER_TOKEN"
```

A Codex desktop app or IDE extension launched from a menu or the Dock may not inherit
the variable your shell exported. Then write the header statically in
`~/.codex/config.toml` instead:

```toml
[mcp_servers.auto-browser]
url = "http://127.0.0.1:8000/mcp"
http_headers = { "Authorization" = "Bearer <token>" }
```

Either way, set it again after the token is regenerated (a deleted `data/api-token`
or a new `API_BEARER_TOKEN`): export the variable afresh, or edit the header.

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
      "serverUrl": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer <contents of data/api-token>" }
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
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer <contents of data/api-token>" }
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
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer <contents of data/api-token>" }
    }
  }
}
```

Without the header, the client reports a 401 "Missing or invalid bearer token".
A controller started with `API_ALLOW_TOKENLESS_LOOPBACK=true` needs no header, but
then any container on its compose network can drive the browser too. With
`REQUIRE_OPERATOR_ID=true`, also send `X-Operator-Id` (or
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
        "AUTO_BROWSER_TOKEN_FILE": "/ABSOLUTE/PATH/TO/auto-browser/data/api-token"
      }
    }
  }
}
```

The bridge reads the token file on every request, so it can start before the
controller has written it. Working from a repo checkout without uv? Point
`command` at `python3` with `args: ["/ABSOLUTE/PATH/TO/auto-browser/scripts/mcp_stdio_bridge.py"]`
instead; that wrapper reads its own checkout's `data/api-token`, so it needs no
token setting.

The bridge handles one request at a time: it reads a line, waits for the
controller's answer, writes it, and only then reads the next, so a slow tool
call (such as a long page load) holds up everything behind it, and a
cancellation the client sends meanwhile is read only after the slow call
returns. Each request to the controller times out after 60 seconds without an
answer by default; change that with `--timeout-seconds` or
`AUTO_BROWSER_HTTP_TIMEOUT_SECONDS`. Both the PyPI package and
`scripts/mcp_stdio_bridge.py` behave this way. A client that needs parallel
calls should connect over HTTP instead.

Bridges older than the one that added `--token-file` ignore
`AUTO_BROWSER_TOKEN_FILE`, and a 401 from one does not mention it. Use
`"args": ["auto-browser-mcp@latest"]`, or set `AUTO_BROWSER_BEARER_TOKEN` to the
file's contents, which every bridge understands.

If you set `API_BEARER_TOKEN` on the controller, put it in
`AUTO_BROWSER_BEARER_TOKEN`; a non-empty one wins over the file. If the controller sets
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
claude mcp add --transport http --scope user auto-browser http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $(cat data/api-token)"
claude mcp add ...   # your search server, as its docs describe
```

Or in a JSON client config, one entry per server:

```json
{
  "mcpServers": {
    "auto-browser": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer <contents of data/api-token>" }
    },
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

Minimal resource listing over the HTTP transport (with `AUTH` holding the token
header, as in the raw tool-call example below):

```bash
curl -s "${AUTH[@]}" http://127.0.0.1:8000/mcp \
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
- `browser://audit/events` for recent audit events (never notified, so read it
  with `resources/read`: subscribing to it returns `-32602`)
- `browser://<session-id>/dom`, `browser://<session-id>/screenshot`,
  `browser://<session-id>/console`, and `browser://<session-id>/network` once a
  session exists

Read a resource by URI:

```bash
curl -s "${AUTH[@]}" http://127.0.0.1:8000/mcp \
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
curl -s "${AUTH[@]}" http://127.0.0.1:8000/mcp \
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

curl -N "${AUTH[@]}" http://127.0.0.1:8000/mcp \
  -H 'accept: text/event-stream' \
  -H "MCP-Session-Id: $MCP_SESSION_ID" \
  -H "MCP-Protocol-Version: $MCP_PROTOCOL_VERSION"
```

When a browser event notifies a resource you subscribed to, the stream emits a
JSON-RPC notification named `notifications/resources/updated` with its `uri`.
Only the events listed in
[`examples/mcp-resources.md`](../examples/mcp-resources.md#subscribing-to-changes)
notify, each for the URIs listed there; for anything else, read the resource
again.

An MCP session has one live stream: a new `GET /mcp` ends the older one, and
`DELETE` or the session's eviction ends it. Notifications are not replayed, so
after reconnecting read again what you watch.

## MCP tool profiles and packs

Every tool the controller lists costs the model context on every request, so the
controller serves a selection fixed at startup. `MCP_TOOL_PROFILE` picks a profile;
`MCP_TOOL_PACKS` adds packs by name or removes them with `-name`.

| Profile | Packs | Tools | `tools/list` size | For |
|---|---|---|---|---|
| `lean` | `browse` | 7 | about 13 KB | the browsing loop: observe, act, read the page, hand to a person, close |
| `curated` (default) | `browse`, `sessions`, `tabs`, `downloads`, `auth_profiles`, `wait`, `scripting`, `vision` | 20 (21 with a vision model) | about 29 KB | a browsing agent: adds sessions with options, saved sign-ins, tabs, downloads, waits and `eval_js` |
| `full` | every pack | 76 (77 with a vision model) | about 79 KB | everything: diagnostics, Witness, admin, the agent queue, the harness |

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
| `scripting` | curated | JavaScript in the page; every call needs an approval, an operator's unless `AUTONOMOUS_APPROVALS=true` | `browser.eval_js` |
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
| `cron` | full | agent runs on a schedule or a webhook | `browser.list_cron_jobs`, `browser.create_cron_job`, `browser.delete_cron_job`, `browser.trigger_cron_job`, `browser.rotate_cron_webhook_key`, `browser.revoke_cron_webhook_key` |
| `harness` | full | the convergence harness | `harness.start_convergence`, `harness.get_status`, `harness.get_trace`, `harness.list_runs`, `harness.list_candidates`, `harness.get_candidate`, `harness.check_drift`, `harness.check_all_drifts`, `harness.graduate` |

```bash
MCP_TOOL_PROFILE=lean                                  # 7 tools, about 13 KB
MCP_TOOL_PROFILE=lean MCP_TOOL_PACKS=sessions,tabs     # 14 tools
MCP_TOOL_PACKS=harness                                 # curated + harness: 29 tools, about 38 KB
MCP_TOOL_PACKS=-scripting                              # curated without eval_js: 19 tools, about 27 KB
MCP_TOOL_PROFILE=full MCP_TOOL_PACKS=-harness,-cron    # 61 tools, about 64 KB
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
(`fast` or `governed`; any other value is refused and nothing is queued) and
`browser.resume_agent_job` resumes interrupted,
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

Pass `workflow_profile=governed` on a tool call (it defaults to `fast`; a tool assigned a governed kind refuses any
other value) and every tool assigned a
governed kind asks for an operator's approval first (most tools that write have one, not all: creating a
session or a proxy persona does not; see `FORCE_GOVERNED_APPROVALS` below): the call returns
`approval_required` with an `approval_id`, an operator approves it, and the agent repeats the same call
with that `approval_id`. An approval covers exactly one call with exactly those arguments (a browser
action's `reason` and `confidence` aside), and the call spends it when it claims it, before the action
runs. If the call then fails, the same `approval_id` is refused with "was spent when its action started
(executed_at ...), so the action may have taken effect: observe the page, or check the outcome, before
acting again. Only to do it again, repeat the call without approval_id, which asks an operator for a new
approval", unless the answer carries `approval_given_back` (a browser action that ended before it began;
below). A spent approval does not mean its action failed, so an operator asked to approve the repeat
checks the outcome too.

#### When an approval is spent, and when it is given back

The claim marks the approval `executed`, persisted, and it stays spent whatever happens next: a refusal
once the action has begun, retryable ones included (`element_not_stable`, `element_covered` and the
other waits inside an action, a timeout, a page that went away); a cancellation once the action has
begun; a crash or a restart. A governed tool call that is not a browser action (`browser.eval_js`,
`browser.set_cookies`, ...) spends its approval once the call is made, however it ends, a refusal or a
cancellation included.

The exception is a browser action that ends before it begins, wherever it runs: `browser.execute_action`
(with or without a `governed_approval_id`), `browser.execute_approval` and `POST
/approvals/{id}/execute`, REST `POST /sessions/{id}/actions/execute`, the agent loop (`/agent/step`,
`/agent/run`, queued agent steps and runs, cron runs), and an upload (`POST
/sessions/{id}/actions/upload`, or an `upload` action through any of these) before its transfer. On
those paths any error the action's pipeline raises before the action begins gives the approval back: its
refusals (the current page on a host `ALLOWED_HOSTS` does not allow, a hosted witness that is not ready,
a witness policy block, an upload's `element_id` that is not a file input or whose one-time marker the
page moved, an operator's reject while the action waits, below), or another failure by then, such as the
page snapshot it takes first. So do any refusal of an approval check or claim inside the call (a
governed `browser.execute_action` with a `governed_approval_id` whose runtime approval is still to be
asked for, unknown, or not approved); a cancellation before the action begins, during its claim's write
included (an agent job cancelled, a harness run past its `max_wall_seconds`, or the controller shutting
down); and any ending before the action begins while an operator's reject waits on it, which then
applies. So does what the dispatched action raises before its pipeline begins, its own session lookup
and an upload's file check included, and a `navigate` that `ALLOWED_HOSTS` refuses after its approval
was claimed (below). Any other error raised outside the pipeline before the action begins, with no
reject waiting, leaves it spent. The approval goes from `executed` back to `approved` (`executed_at`
cleared, audited as `approval_given_back`), or to `rejected` when the operator's reject applied. A
give-back revokes the claim before it writes anything, so a task still inside the execution cannot act
on the approval it returns, and a give-back whose audit fails leaves the approval usable. The error or
the `approval_required` answer carries `approval_given_back` (the approval the call was given, the outer
one when there are two) and `approvals_given_back` (every approval given back, innermost first), each
with its `status` (`approved`, `rejected` or `expired`) and a message, and an agent loop step carries
them in its `execution`. Retry an `approved` one with the same `approval_id` once the cause is gone,
rather than asking again, and repeat the call without it for an `expired` one. A session that has closed
never comes back: its note says the approval is still approved but nothing can run it ("... repeat the
call in a live session without approval_id, which asks for a new approval").

An action approval covers one operation: a second one under the same claim, or one from a task still
running after the claim was released, is refused before it changes anything. Its expiry is checked at
the claim, so an action that started in time is not refused once it has run. A policy block before the
operation began does not navigate back (that made an approved `go_back` go back twice), and neither does
an approval refusal that stops an upload before its transfer. After an approved `go_back` or `reload`, a
policy block does not go back either: the page stays where the action left it, and a page
`ALLOWED_HOSTS` refuses is sent to `about:blank`. The approval guard's own refusals reach the agent and
the audit with their code (`approval_released`, `approval_rejected`, `approval_already_used`); the
blocked action's audit entry carries it as `details.code`, as a policy block's carries
`browser_action_blocked`. A witness receipt reports an approval status only for the approval its action
ran under: `executed` once the action began, `approved` when it was refused before and given back,
`rejected` when an operator's reject stopped it (which gets its own receipt), and none for an
`approval_id` passed with an action that needed none. A governed `browser.execute_action` that spends
its `governed_approval_id` names that approval in the receipt. The action's own status stays `blocked`
or `failed`, and a policy block after the change names the block under `target.after_change`.

A governed upload's file is checked before anyone is asked to approve it, in `browser.execute_action`
and the agent loop: a missing file is refused (`not_found` over MCP) and queues nothing.
`browser.execute_approval` and `POST /approvals/{id}/execute` check an upload's file before they claim
its approval, so a file gone since the approval leaves it approved. For every action they also refuse an
approval whose session has closed before they claim it, and it stays `approved`. A session that closes,
or an upload's file that is removed, after the approval is claimed and before the action begins counts
as an action that ended before it began: the approval is given back, and the note for a closed session
says nothing can run it. A `navigate` to a host `ALLOWED_HOSTS` refuses is refused before an approval is
asked for, in every profile; one whose host leaves the list after the approve is refused when it is
executed, and gives its approval back.

A tool call's approval is spent only by repeating that tool call: `POST /approvals/{id}/execute` runs
approved browser actions, and refuses a tool call's approval, or a takeover approved before approvals
recorded their origin, with a 409 without spending it. An agent cannot ask for approval of a
`request_human_takeover` or a `done` action, or run one: it is refused with a 400 before anything is
queued, whatever risk category it carries; over MCP the error for a takeover points to
`browser.request_human_takeover`, and the one for `done` says it ends an agent's run.

With `STATE_DB_PATH` set, every record is written to the file store first, and to SQLite while SQLite is
primary, and each write lands whole before the store's lock is released, also when its caller is
cancelled. A start on SQLite first copies into it every file record it lacks or holds differently, so an
approval spent while SQLite was unavailable stays spent, except a file record that would turn SQLite's
spent or rejected one to another status. SQLite's spent record always stays: a later file record beside
it is a give-back SQLite never took (cut off by a crash, a write whose put-back failed too, several
workers), and losing it leaves the approval spent, so the agent asks again. Its rejected record stays
over a file record not updated after it, and an approve made after the reject while SQLite was
unavailable is copied: operator decisions and give-backs are stamped later than the record they change,
by a microsecond when the clock has not moved past it. So the copy can only err toward spent or
rejected. The record SQLite keeps is first written over the file's, so a later start whose SQLite fails
cannot claim the approval again. If that write fails, it is logged with the approval's id and the start
goes on with SQLite primary; if SQLite then cannot take the file store's other records either, the
controller refuses to start rather than run on a file store that could claim a spent approval, and the
error names those approvals and says to check that `APPROVAL_ROOT` and `STATE_DB_PATH` are writable. Any
other failure of the copy, or of SQLite's own start, leaves the file store primary, and the log says
which step failed. A write whose SQLite half fails puts the file record back before the failure is
reported, so both stores hold what the caller was told; if putting it back fails too, that is logged,
and a give-back reported as failed is still not revived at the next start, since SQLite's spent record
stays. Within one process, writes under the lock land in order; records stored by 1.11.1, several
workers sharing a store, and a restored backup can leave a file record that differs without being the
latest word, and the rule then keeps the spend or the rejection. A stored record that no longer
validates (a navigate approval an earlier version stored for a URL this one refuses) is left out of
listings and answers as an unknown id, in either store; fetching or listing it logs a warning that names
it and where it fails, not what it holds. An approval the retention has deleted
(`APPROVAL_RETENTION_DAYS` after its decision, 30 by default; see `docs/production-hardening.md`)
answers as an unknown id too: a 404 from the approval routes, and for a call that presents its
`approval_id` the error an id that never existed gets. Not covered: two controller processes sharing one
approval store (file or SQLite) can both claim an approval, since the claim is atomic only within a
process; several workers or replicas are not supported.

These refusal messages are the tool call's answer over MCP, `browser.execute_approval` included. REST
routes answer an approval refusal with fixed text (403 "Not permitted" on `/sessions/{id}/actions/*`,
409 "Conflict" on `POST /approvals/{id}/execute`); a browser action's own error keeps its status, its
message, its `code` and its `approval_given_back` there too (the approval guard's refusals,
`approval_rejected` and the like, answer 409), and a 409 for a decision on a claimed approval, or for an
approve whose arguments or text are gone, carries its reason as the `detail`, which the dashboard shows.
So does the 409 that refuses an operator listed in `APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS` an approve
of an approval they asked for themselves: the approval stays as it was, for another operator to approve
where one can reach it, in a session nobody owns (in a session the asker owns, and in controller scope,
none can, and a pending one stays pending).

#### Rejecting or approving while an action holds its approval

An operator who rejects an approval while a browser action holds it but has not begun (it waits for the
session, or its checks are running) gets the record back, still `executed`, with a `message`: the
rejection stops the action, which is refused with `approval_rejected`, and the approval becomes
`rejected`, with the operator's comment, a witness receipt and an audit entry, however the action ends
before it begins (an error or a cancellation included; if the
approval cannot be written back, it stays spent, and the audit says the rejection did not apply).
Approving it meanwhile withdraws the rejection, and the answer says what follows: the action may start;
it was already refused, and the approval goes back to approved (or has expired, and the agent asks
again); or it ended with an error that gives no approval back by itself, and the approval stays spent.
Which ending gives back is read when the approval is written back, so it does not depend on whether the
approve landed just before the action ended or just after. Once the action has begun, or while a
governed tool call that is not a browser action runs, a decision is refused with why (a 409 over REST,
with that text), and so is an agent's own reject of a claimed approval. The claim lives in the
controller process, so a decision sent to another worker or replica while an action holds the approval
is refused with a 409 ("has already been spent"), where 1.11.1 accepted a reject. On a Windows
host with the default file store (`STATE_DB_PATH` unset), any read or write of a record made while
another write replaces it can fail, since Windows briefly locks a file being replaced: a decision then
answers 409 "Conflict" too; a claim, a check of the approval before the action runs, or a give-back
fails the call; and a listing leaves the record out. A give-back that fails so leaves the approval
spent. The Docker image runs Linux, where this does not happen.

#### What the operator is shown

For an action approval the reason is the agent's own words, so read the action as well as the reason: a
request for a real action can still be described misleadingly. The dashboard queue shows the action
itself, uncut: one list of every field the approval binds, checked against the match that binds it. That
is the element id that runs first (a selector beside it is marked as not used), a navigate's URL, an
upload's file path, the key a press sends, the text a `type` types and whether it replaces the field,
coordinates, an option's value, label or index, a scroll's distance, a wait's length, and any recipient,
platform or username set, each string JSON-quoted, so a newline shows as `\n` and a tab or another
control as its JSON escape (`\t`, `\u0001`). A field that does not act is marked "(not used by click)"
when the action does not read it (a `url` on a click, a `file_path` or `text` on a press), or "(not
used: selector runs)" when another field set beside it runs instead (coordinates beside a selector are
marked so; an option's index runs before its value, and its value before its label). A `select_option`
value that runs is marked "(matches an option's value or label)", since it picks the first option whose
value or label it is, as Playwright matches it, and an empty value or label is shown (`value=""`). The
approval webhook (`APPROVAL_WEBHOOK_URL`) and the SSE approval event carry its `origin` and an
`action_summary` the server writes from the same fields, which cuts a string at 200 characters and says
how many it leaves out: when it does, decide on the queue row. The approve dialog cuts the same way,
names the target first and points to the row; a tool call's dialog shows the server's preview.

For a tool call the reason previews the arguments (600 characters, credentials left out by tool and
path, below; a cut preview ends before an escape it would split, and gives the length of the whole
text); the record, the webhook and the SSE event (sent only for an approval in a session) carry
`arguments_size`; and `GET /approvals/{id}/arguments`, or the queue's "View all N characters", shows
them whole, with the same credentials left out, to whoever may read the approval. They are kept as
`<id>.arguments` in `APPROVAL_ROOT`. The record's `arguments_sha256` is that text's digest, not the one
the approval binds, which covers the call's own arguments (so a cookie's or a storage value is approved
without being viewable). The file is removed when the call that spends the approval ends, or at the next
start for a call whose process died, or whose removal failed (that is logged, and leaves the file), and
for a file no record names. The retention removes it too, ahead of the record, when it deletes an
approval: at a start and in each maintenance sweep, once the decision is older than
`APPROVAL_RETENTION_DAYS` (`0` deletes nothing). Until then it stays, and nothing else sweeps it: an
approval left pending keeps its file at any age, and one approved and never run keeps it until it has
expired and its decision is that old (expiry is not a final state in the store: one past its
expiry stays `approved`, and is refused when used); a rejected one keeps it for the approval TTL
(`APPROVAL_TTL_MINUTES`) after the rejection, since an operator may approve it again meanwhile, and the
file goes at the next approval request or start after that. An approve of one whose arguments are gone
is refused before the record changes ("its arguments are no longer stored; the agent asks again"), and
so is an approve of a sensitive `type` whose text the controller no longer holds, dropped by a reject, a
restart, or 24 hours after it was asked, or held by another worker ("its text is no longer held; the
agent asks again"). When the agent asks again with the same text while that approval is still pending in
the same controller process (the 24-hour case), the text is held again and that approval can be
approved. After a reject, a restart, or on another worker, asking again queues a new approval for the
operator to decide, and the old one can no longer be approved.

The digest a tool call's approval binds is kept keyed. The record's `action.text` reads
`<tool> hmac-sha256:<key id>:<digest>`: an HMAC, under a key only the controller holds, over the tool's
name and the SHA-256 of the call's own arguments, so a value the preview leaves out cannot be guessed
against what `GET /approvals`, the approval tools or the `approval_required` answer serve. The key is 32
random bytes, written in hex at the first start to `.tool-call-digest.key` in `APPROVAL_ROOT` (there too
when `STATE_DB_PATH` holds the records), created with mode 0600, read at every start and never
replaced; whoever can read `APPROVAL_ROOT` itself reads the key with the records. Keying stops a guess
made offline, against a listing or a copy of the records without the key, and not one made through the
controller: the keyed text is the same for the same tool and arguments in every session (`session_id`
is no part of it), so a caller who can read an approval and may make governed calls can still try a
value one request at a time. A try made without `approval_id` leaves a pending approval and the record
of its request (a witness receipt in a session, while `WITNESS_ENABLED` is on; an audit event in
controller scope). A try that presents an approved `approval_id` with other arguments leaves nothing: a
miss is refused ("does not match the requested action") and writes no approval, receipt or audit event,
and the right value runs the approved call and spends the approval.

Records that 1.17.0 and earlier stored with the plain SHA-256 are rewritten at the first start that
finds them, in both stores, and cover the same call as before; one that cannot be rewritten is logged,
still matches its call, and is taken at the next start. Until then, or until a decision or a call writes
that record, what is stored of it (its file, and its row with `STATE_DB_PATH`) holds the plain digest,
and the controller lists and answers it keyed all the same.

An approval kept under a key the controller no longer holds (the file was removed or replaced before a
start; a running controller goes on with the key it read) cannot be matched to any call. Approving it is
refused before the record changes ("its arguments' digest is kept under another key than this controller
holds ..."), a call that presents one approved earlier is refused with the same reason and told to
repeat the call without `approval_id`, which asks for a new approval, and each start logs how many such
approvals are pending or approved; rejecting them ends that warning. An operator's rejection kept under
that key no longer stops an agent's own approval of the same call under `AUTONOMOUS_APPROVALS`, and each
start logs, in a warning of its own, how many such rejections are stored (one made to end the first
warning is one of them): reject the call again when it is asked for, turn `AUTONOMOUS_APPROVALS` off, or
restore the key file. That warning repeats while the rejections are stored. The retention removes each
one once it is older than `APPROVAL_RETENTION_DAYS`, in controller scope and in an open session too,
where it keeps an operator's rejection that still matches its call: one kept under a key that is gone
refuses nothing. Only a process that holds the file's key does so (a start that could not use the file
is described below). A start counts them before its own retention pass, so the start that removes the
last of them logs the warning once more. With `APPROVAL_RETENTION_DAYS=0` nothing removes them, and the
warning repeats until the key file is restored. A key file restored after the retention has removed a
rejection does not bring it back: reject the call again when it is asked for. When the file cannot be
used at a start (it holds no key, cannot be read, or cannot be written
where there is none), the start logs it and keys with a key of its own: the tool call approvals asked for
in that process work until it ends, and are refused the same way after a restart. To that process every tool call
approval stored under a key before reads as kept under another key, the file's own among them, so its
retention removes no operator's rejection on that ground: one in controller scope or in an open session
stays whatever its age, and the next start that can use the file matches it again. A record that still
holds the plain SHA-256 is not rewritten by such a start, which logs how many it left, nor by a decision,
a claim or a give-back of it in that process: under that process's key it would match no call at any
later start. It matches its call in that process as before, and the next start that can use the file
rewrites it under the file's key with what was decided of it there: an operator's rejection made there
still stops the agent's own approval of the call, and an approval made there can still be run until it
expires. A file that holds no key
is left as it is, and has to be removed before a start writes a new one. A start that dies between creating the
file and writing the key leaves one, empty or cut short of its 64 hex characters: no approval was ever
kept under it, so it is safe to remove. One that held a key and was changed since (edited, or saved in
another encoding) is better restored, since approvals are kept under the key it held.

In the dashboard (the queue, the confirm dialog, the arguments viewer and the run replay), in the
`action_summary`, and in the webhook's and the SSE event's `reason`, a character that would hide or
reorder the text is shown as its code point (`[U+202E]`): format characters such as bidi overrides and
zero-width marks, controls, line and paragraph separators, and default-ignorable code points such as
variation selectors and Hangul fillers. The server's lists of format and default-ignorable code points
are Unicode 17.0's, whatever Unicode its Python knows. In the `reason`, a newline is written `\n`, and a
tab, a carriage return or another control as its code point (`[U+0009]`), so a multi-line reason
arrives on one line; field strings are JSON, so their controls show as JSON escapes (above). A tool
call's preview and arguments are ASCII JSON (`\u202e`). Every answer that carries the record serves such
characters as they are: the record and listing routes (`GET /approvals`, `GET /approvals/{id}`, MCP
`browser.list_approvals`), the answers to an approve, a reject or an execute over REST and MCP, the
`approval_required` answer, and witness receipts. The queue lists only pending approvals: read one on
its row before you decide. Letters that look alike across scripts (a Cyrillic `а` for a Latin `a`), and
right-to-left letters, are shown as they are, in the queue, the summary, the webhook and SSE alike. A
navigate's `url` is shown with its host as a browser sends it (each non-ASCII label as `xn--`) and the
host as written beside it, in the queue and the summary; a credential in it is shown.

A preview leaves out credentials by tool and path: a `browser.set_cookies` preview shows each cookie but
its value, and a `browser.set_local_storage` value shows only its length. Five URL fields are checked
and shown as a browser reads them: `browser.cdp_attach`'s URL, a cookie's URL, a harness run's start URL
and its mock final page's URL, and `browser.create_cron_job`'s `start_url`. Each is shown without its
userinfo, and without the value of each query parameter whose name, or a part of it split at punctuation
and at camelCase, names a credential (token, key or apikey, secret, password, passwd or pwd, sig or
signature, session, sessionid, jsessionid or phpsessid, sid, ticket, code, auth or authorization, otp,
jwt, credential or credentials, or the whole name SAMLResponse; such as `access_token`,
`X-Amz-Signature`, `sig`, `ticket` or `code`, so a `promo_code` is left out too), or that runs such
words together (`accesstoken`, `clientsecret`, `privatekey` and the like), the name decoded until it
reads the same (at most twice), or of a query item with no `=`; a part, not a substring, so `monkey` is
shown. `&` and `;` both separate items. A query value that is itself an http(s) URL as a browser reads
one (past spaces and control characters at either end, without tabs or newlines, and with or without the
`//` after its scheme, or with extra slashes), such as a return URL, has its own userinfo and credential
values left out (one level deep); one that urllib cannot read as a URL (`https://[draft`) is shown as
written. Every other query value is shown, a hosted browser's `--proxy-server` among them. A credential
in a URL's fragment, in a query parameter of another name, or anywhere else in the arguments is shown.

Those five fields are refused when one holds a backslash or a control character (U+0000 to U+001F; DEL
and other controls are not refused), or names one host to a browser and another to the controller: a URL
written without the `//` after its scheme or with extra slashes (`https:u:p@host/`,
`https:////u:p@host/`); a percent-encoded host; a host a browser maps to another name beyond case
folding and composition, mapped by UTS 46's table as a browser maps it, such as `127.1` in full-width
digits or a name with a zero-width space in it (a host it maps only by case folding or composition, such
as a name written decomposed, or with the fi ligature, the long s or the micro sign, is accepted); and
an IPv4 address not written as a dotted quad, such as `2130706433` or `127.1`. So
`wss://x@evil.example\@bank.example/`, which the controller would read as `bank.example` while
Playwright connects to `evil.example`, is refused. An internationalized name is accepted, and shown as
the `xn--` form a browser sends, with the name as written beside it.

A navigation URL (a `navigate` action, `POST /sessions/{id}/actions/navigate`, `POST
/sessions/{id}/tabs/open`, and the `start_url` of `browser.create_session`, `browser.fork_session` and
the REST session and fork routes, in every profile) is refused by the same host rules, but not for a
backslash or a control in its path or query, which moves no host (a search for `C:\Users`, say): only
when the host a browser would load differs from the one shown. A control character (U+0000 to U+001F) in
its scheme, the slashes after it or its host is refused ("has a control character in its scheme or host,
which a browser drops"): a browser drops a tab or a newline there, and the URL shown would keep it. A
cron job that an earlier version stored with a `start_url` these rules refuse fails each run before a
browser opens, and the controller logs why.

A `type` the agent marked sensitive reads "text hidden at the agent's request (sensitive)":
the agent can mark any text sensitive, so look at the field it types into. One it did not mark shows
its text only where every field the text could go to read as plain when the approval was asked for
(`field_read: "plain"`): the element the `type` names where it takes text (an input that text is typed
into, a textarea, a select, an editable element), the control of a `label`, and the fields inside
either, through open shadow roots. Where one of them read as secret (a password field, say) it reads
"text hidden: its field reads as secret" (`field_read: "secret"`), and stays so. It reads "text hidden:
its field has not been read" (`field_read: "unread"`) where there is no such field (a component that
takes its keys itself, or keeps its field in a closed shadow root), where a frame is among what is
named (its document is not read), where the read did not answer within a second (a field not on the
page yet), and for any pending `type` approval stored before 1.18.0, until the same action is asked
for again and its read says plain or secret.
`GET /approvals` answers that text for it; the approval keeps the text the action must be retried
with. An operator's rejection
stops the agent from approving the same request itself (`AUTONOMOUS_APPROVALS=true`) only within its
session, or in controller scope for the same operator; with `AUTONOMOUS_APPROVALS=true` the agent
approves its own calls, and can repeat a rejected one in a new session.

#### Controller-scope approvals and the audit log

A governed tool that names no session (`browser.delete_memory_profile`, `browser.cdp_attach`)
is approved in a controller-wide scope: the approval's `session_id` is `controller`, and
`GET /approvals?session_id=controller` lists the ones you may see. With no session there is no
session owner, so under named credentials (`API_BEARER_TOKENS`) such an approval belongs to
the operator who asked and to nobody else: another operator cannot list, decide, reuse or
spend it. `POST /approvals/{id}/execute` does not run it (the agent repeats the tool call).
Witness receipts and audit entries for an approval carry its origin and, for a tool call, the tool.
The audit log is operator-wide, though (`GET /audit/events`, `browser://audit/events`): any
operator reads in it another operator's approval ids, kinds, origins and tools, and the
comments decisions were given with, though not a reason or a tool call's arguments.

#### Cron jobs and FORCE_GOVERNED_APPROVALS

The cron tools are governed too: `browser.create_cron_job`, `browser.trigger_cron_job` and
`browser.rotate_cron_webhook_key` are `write`, and `browser.delete_cron_job` and
`browser.revoke_cron_webhook_key` are `destructive`. In `tools/list`, the rotate tool's annotations say
`destructiveHint: true` all the same, as the delete and revoke tools' do: it ends the working key at
once, so a client that runs tools without asking when they say they destroy nothing asks first. A job
keeps the `workflow_profile` of the call that
created it, and every run, scheduled or triggered, starts under it, so a job created by a `governed`
caller cannot be used to run writes without approval. Likewise, a run or step queued by a `governed`
`browser.queue_agent_run` or `browser.queue_agent_step` call runs governed, whatever its request names.
These three tools refuse a `workflow_profile` (or `policy_profile`) other than `fast` or `governed`, as
every governed tool does, and create or queue nothing: read as `fast`, a mistyped value stored or queued
`fast`. `policy_profile` is a second name for the same argument, for these tools and for every governed
one: a call that gives both is `governed` when either says `governed`, and is refused when either holds
another value (`null` reads as left out). The two queue tools are not governed: their `tools/list` schema
shows `workflow_profile`, with what it does there, and no `approval_id`, since queuing a run or step asks
for no approval.
A job created through REST (`POST /crons`) stores the `workflow_profile` the request body names, `fast`
by default. A job created before this release stores `fast`: it keeps running as `fast` unless
`FORCE_GOVERNED_APPROVALS=true` (below). A job's profile is fixed when it is created (jobs cannot be
edited). To govern an existing job, delete it and create it again from a `governed` caller (or through
REST with `"workflow_profile": "governed"`), or turn on `FORCE_GOVERNED_APPROVALS`.

A governed job's run cannot continue past an action that needs an operator's approval: the run stops
there, and its session closes with the run, so that approval, once approved, can no longer be executed:
it stays `approved`. Triggering the job again asks for a new approval in a new session. With
`AUTONOMOUS_APPROVALS=true` the agent approves such an action itself and the run carries on. Run work
that needs an operator's approval in an interactive session, or keep the job `fast` under a reviewed
script.

`workflow_profile` is the caller's argument, so by default governed approvals are the agent's choice. A
deployment that wants them as a rule sets `FORCE_GOVERNED_APPROVALS=true`: every tool with a governed
kind then runs as `governed` whatever the call passed, with or without a session, and `fast` is no
longer available for them. Tools without a governed kind are unchanged: the reads, and some that write,
among them the proxy persona tools; `browser.create_session` and `browser.fork_session` open a
`start_url` with no approval, and so does each run of a cron job, scheduled or triggered. Only the MCP
cron tools ask for an approval to create or trigger a job; REST `POST /crons` and `POST
/crons/{job_id}/trigger`, which need the bearer token, never do. `FORCE_GOVERNED_APPROVALS` also forces
every step of the agent loop to `governed`, whatever `request.workflow_profile` says, so
`browser.queue_agent_step` and `browser.queue_agent_run`, REST `/agent/step` and `/agent/run`, and cron
runs cannot be used to get around it.

It does not cover the REST action routes (`/sessions/{id}/actions/*`): they ask for approval
only for uploads and for `post`, `payment`, `account_change` and `destructive` decisions, and an
agent holding a bearer token can call them directly. The setting is a control on agents that
act through MCP tools and the agent loop; it is not a sandbox for a token holder. It is off by
default because an agent loop written for `fast` stalls on its first write until an operator
approves. With `AUTONOMOUS_APPROVALS=true` the agent can approve its own call, which cancels
the effect. See `docs/production-hardening.md`.

## Raw tool-call example

If you want to see the shape of the tool surface without wiring up a full MCP client yet
(from the auto-browser folder, so `AUTH` carries the token):

```bash
AUTH=(-H "Authorization: Bearer $(cat data/api-token)")

curl -s "${AUTH[@]}" http://127.0.0.1:8000/mcp/tools | jq

curl -s "${AUTH[@]}" http://127.0.0.1:8000/mcp/tools/call \
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
