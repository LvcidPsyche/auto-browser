# Changelog

All notable changes to auto-browser are documented here.

## [Unreleased]

## [0.5.2] — 2026-03-31

### Fixed

#### `make doctor` sandbox preflight
`scripts/doctor.sh` now fails fast with a clear message when the current shell cannot
open local sockets (for example, a sandboxed agent session). This avoids repeated
Python `PermissionError` tracebacks during port probing and points contributors to
rerun the readiness smoke from a normal terminal or an elevated session.

#### Local developer Python preflight
Host-side controller entrypoints now require Python 3.10+ up front and print a direct
compatibility message when only an older interpreter is available. This aligns local
controller workflows with the runtime and avoids late failures from Python-version drift.

#### Host-side controller test path
Added `make test-local` plus editable package metadata for `./controller`, making it
possible to run the controller test suite on a host Python 3.10+ environment without
going through Docker every time.

#### Provider HTTP coverage and broader linting
CI now exercises host-side controller tests, includes HTTP coverage for `/agent/providers`
and `/sessions/{id}/agent/step` without real provider credentials, and widens Ruff checks
to cover controller tests and Python helper scripts with import sorting.

#### `browser-node` Xvfb restart cleanup
The browser-node entrypoint now clears stale `:99` X lock/socket files before starting Xvfb,
preventing rerun failures where Playwright launched before an X server was actually available.

## [0.5.1] — 2026-03-26

### Fixed

#### Shared `utc_now()` utility
`_timestamp()` was duplicated identically in five modules (`audit.py`, `approvals.py`,
`agent_jobs.py`, `browser_manager.py`, `session_tunnel.py`). Extracted to `utils.utc_now()`.
Corrected the screenshot filename site to use the compact `strftime` format it always required
(ISO-8601 is not suitable for filesystem paths).

#### `tool_inputs.py` module split
`tool_gateway.py` mixed ~280 lines of Pydantic input model class definitions with dispatch
logic. All input models are now in a dedicated `tool_inputs.py` module. `tool_gateway.py`
re-exports them for backwards compatibility.

#### `agent_jobs.py` cleanup
- Deleted dead `hasattr(store, 'update_status')` guard that was always `False`.
- Merged duplicate `enqueue_step` / `enqueue_run` into shared `_enqueue()`.

#### `orchestrator.py` exception handler merge
Two 90%-identical `except ProviderAPIError` + `except Exception` branches merged into one
with an `isinstance` check for error-code derivation.

#### `mcp_transport.py` exception narrowing
Overly-broad `except Exception` on JSON parse boundary narrowed to `except ValueError`.

#### `approvals.py` hardening
- SQLite WAL mode + `PRAGMA synchronous=NORMAL` for concurrent read performance.
- Silent `except Exception: continue` in `FileApprovalStore._list_sync` now logs at DEBUG.

#### `cron_service.py` atomic writes
`_save()` replaced `write_text()` with tmp-file + rename to prevent corrupt-store-on-crash.

#### `models.py` — `_WithApproval` mixin
Nine social action request models and `UploadRequest` all repeated `approval_id: str | None = None`.
Extracted to `_WithApproval` base class.

#### `session_store.py` — `_MarkInterruptedMixin`
`mark_all_active_interrupted` was implemented identically in both `FileSessionStore` and
`RedisSessionStore`. Extracted to shared `_MarkInterruptedMixin`.

#### `network_inspector.py` — pending dict memory leak
When a page is detached (tab close, browser crash), in-flight requests that never received
`requestfailed` / `requestfinished` events would accumulate in `_pending` indefinitely.
`detach()` now schedules `_flush_pending()` which drains all pending entries as `failed` with
`failure_text = "session detached"`.

#### `browser_manager.py` — `create_session` decomposition
190-line `create_session` method split into four focused private helpers:
`_check_session_limit`, `_prepare_session_dirs`, `_build_context_kwargs`,
`_cleanup_failed_session`.

#### `main.py` — global `KeyError → 404` handler + route simplification
A `@app.exception_handler(KeyError)` handler was added so all store-layer `KeyError` raises
automatically return `404`. Removed redundant per-route `except KeyError` blocks across
~30 route handlers, reducing main.py by ~120 lines.

---

## [0.5.0] — 2026-03-25

### Added

#### CDP Connect Mode
`POST /sessions/cdp-attach` and `browser.cdp_attach` MCP tool — attach to an existing Chrome
instance that is already running with `--remote-debugging-port`. Useful for connecting to a browser
the user already has open, or a browser managed by another process.

#### Network Inspector
Per-session request/response capture via Playwright's CDP event bridge.
- Captures: method, URL, resource type, status, timing, headers, body (text only, size-limited)
- `GET /sessions/{id}/network-log` REST endpoint
- `browser.get_network_log` MCP tool (supports `limit`, `resource_type`, `url_pattern` filters)
- Sensitive headers automatically masked (`Authorization`, `Cookie`, `Set-Cookie`, `x-api-key`)
- PII scrubbing applied to request/response bodies
- Config: `NETWORK_INSPECTOR_ENABLED`, `NETWORK_INSPECTOR_MAX_ENTRIES`, `NETWORK_INSPECTOR_CAPTURE_BODIES`, `NETWORK_INSPECTOR_BODY_MAX_BYTES`

#### PII Scrubbing Layer
Comprehensive multi-layer sensitive data redaction throughout the pipeline.
- **16 pattern classes**: AWS access/secret keys, JWT tokens, Bearer tokens, PEM headers, API key URL params, password fields, credit cards (Luhn-validated), SSNs, emails, US/intl phones, GCP service account keys, Azure secrets, generic hex tokens, generic base64 secrets
- **Screenshot pixel redaction**: Pillow draws black rectangles over OCR bounding boxes where PII was detected
- **Console log scrubbing**: Applied to all `get_console_messages` responses
- **Network body scrubbing**: Applied to captured request/response bodies
- `GET /pii-scrubber` — live status endpoint (patterns active, enabled flags, scrub stats)
- `browser.pii_scrubber_status` MCP tool
- Config: `PII_SCRUB_ENABLED`, `PII_SCRUB_SCREENSHOT`, `PII_SCRUB_NETWORK`, `PII_SCRUB_CONSOLE`, `PII_SCRUB_PATTERNS` (comma-separated pattern names), `PII_SCRUB_REPLACEMENT`, `PII_SCRUB_AUDIT_REPORT`

#### Proxy Partitioning
Named proxy personas for per-agent static IP assignment — prevents shared network footprints.
- `browser.list_proxy_personas`, `browser.create_proxy_persona`, `browser.delete_proxy_persona` MCP tools
- REST: `GET /proxy-personas`, `POST /proxy-personas`, `DELETE /proxy-personas/{name}`
- Proxy config stored in JSON file (`PROXY_PERSONA_FILE`); passwords never returned in list/summary calls
- Session creation accepts `proxy_persona` param to route through a named proxy

#### Shadow Browsing
Flip a running headless session to a headed (visible) browser for live debugging.
- `POST /sessions/{id}/shadow-browse` — migrates cookies/storage to a new local-headed Playwright instance
- `browser.enable_shadow_browse` MCP tool
- Original session continues running; headed session is a fork with the same auth state
- Config: `SHADOW_BROWSE_ENABLED`

#### Session Forking
Branch a session's current state (cookies + local/session storage) into a new independent session.
- `POST /sessions/{id}/fork` — returns new session ID with full auth state cloned
- `browser.fork_session` MCP tool — optional `name` for the fork

#### Playwright Script Export
Export any session's recorded actions as a runnable Python Playwright script.
- `GET /sessions/{id}/export-script` — downloads `.py` file
- `browser.export_script` MCP tool
- Sensitive typed text replaced with `<REDACTED>` placeholders
- Supports: navigate, click, hover, type, press, scroll, wait, reload, go_back/forward, select_option, open_tab

#### Shared Session Links
HMAC-signed, TTL-enforced observer tokens for team handoffs.
- `POST /sessions/{id}/share` — creates a time-limited share token
- `GET /share/{token}/observe` — read-only session view (screenshot + metadata)
- `browser.share_session` MCP tool
- Config: `SHARE_TOKEN_SECRET`, `SHARE_TOKEN_TTL_MINUTES` (default: 60)

#### Vision-Grounded Targeting
Use Claude Vision to locate elements by natural language description instead of CSS selectors.
- `browser.find_by_vision` MCP tool — `description` + optional `screenshot_path`
- Returns pixel coordinates `{x, y}`, confidence, and `selector_hint`
- Falls back gracefully when `ANTHROPIC_API_KEY` is not set
- Config: `ANTHROPIC_API_KEY`, `VISION_MODEL` (default: `claude-opus-4-5`)

#### Cron / Webhook Triggers
Autonomous scheduled and webhook-triggered browser automation jobs.
- Full CRUD: `GET/POST /crons`, `GET/DELETE /crons/{id}`, `POST /crons/{id}/trigger`
- `browser.list_cron_jobs`, `browser.create_cron_job`, `browser.delete_cron_job`, `browser.trigger_cron_job` MCP tools
- APScheduler for cron expressions (optional install: `pip install apscheduler`)
- Webhook trigger with HMAC key (`webhook_key`) — compare via `hmac.compare_digest`
- Config: `CRON_STORE_PATH`, `CRON_MAX_JOBS`

#### MCP Resources Protocol
Live browser state exposed as MCP subscribable resources.
- Capabilities advertisement: `{"resources": {"subscribe": false}}`
- `resources/list` — enumerates all active sessions and their sub-resources
- `resources/read` — fetches live content:
  - `browser://sessions` → JSON list of all sessions
  - `browser://{id}/screenshot` → PNG as base64 blob
  - `browser://{id}/dom` → page HTML as text
  - `browser://{id}/console` → recent console messages as JSON
  - `browser://{id}/network` → recent network log as JSON

#### Expanded Tool Surface (30+ new MCP tools)
New tools beyond the existing core:
`browser.get_network_log`, `browser.fork_session`, `browser.eval_js`, `browser.wait_for_selector`,
`browser.get_html`, `browser.find_elements`, `browser.drag_drop`, `browser.set_viewport`,
`browser.get_cookies`, `browser.set_cookies`, `browser.get_local_storage`, `browser.set_local_storage`,
`browser.export_script`, `browser.cdp_attach`, `browser.find_by_vision`, `browser.share_session`,
`browser.enable_shadow_browse`, `browser.list_proxy_personas`, `browser.create_proxy_persona`,
`browser.delete_proxy_persona`, `browser.list_cron_jobs`, `browser.create_cron_job`,
`browser.delete_cron_job`, `browser.trigger_cron_job`, `browser.pii_scrubber_status`

### Changed
- `McpHttpTransport` now accepts `manager` param for Resources protocol live data
- MCP server version bumped to `0.5.0`

---

## [0.4.0] — 2026-03-23

### Added

#### Open New Tab
`POST /sessions/{id}/tabs/open` — open a new browser tab in the session's existing context.
- `url` (optional) — navigate to a URL immediately after opening
- `activate` (bool, default `true`) — make the new tab the active page
- New tab inherits cookies and auth state from the session automatically
- Returns updated tab list and session summary

Completes the tab management surface: list (`GET`), open, activate, close.

#### Session Replay View
`GET /sessions/{id}/replay` — dark-mode HTML page for reviewing a session after the fact.
- Screenshot gallery (chronological, sourced from `/artifacts/{id}/`)
- Audit event timeline with timestamp, type, operator, and data excerpt
- Session metadata header (status, title, created time, current URL)

Useful for debugging agent runs and as a demo/handoff surface.

### Fixed
- `AUDIT_ROOT` now included in all test `Settings` instantiations that construct `BrowserManager`,
  resolving `PermissionError: /data` failures in the local (non-Docker) test suite. 149 tests passing.

---

## [0.3.0] — 2026-03-18

### Added

#### Perception Presets
Three observe modes via `preset` query param or `POST /sessions/{id}/observe` body:
- **`fast`** — screenshot only; skips OCR and accessibility tree. Sub-200ms observe loops for tight agent feedback cycles.
- **`normal`** — current default. Screenshot + OCR + accessibility tree + interactables.
- **`rich`** — normal with doubled interactable limit and 4000-char text excerpt for complex pages.

New `POST /sessions/{id}/observe` endpoint accepts `{preset, limit}` body for richer control.
Config: `PERCEPTION_PRESET_DEFAULT` (default: `normal`).

#### SSE Event Stream
`GET /sessions/{id}/events` — Server-Sent Events stream for live session monitoring.
- Events: `observe`, `action`, `approval`, `session`
- Keepalive comments sent every `SSE_KEEPALIVE_SECONDS` (default: 15s) to prevent proxy timeouts
- Global subscriber support for multi-session dashboards

#### Screenshot Diff
`POST /sessions/{id}/screenshot/compare` — pixel-by-pixel diff against the most recent prior screenshot.
Returns `changed_pixels`, `changed_pct`, diff image URL, and source image URLs.
Useful for verifying that an action had visible effect.

#### Approval Webhooks
Set `APPROVAL_WEBHOOK_URL` to receive a signed POST whenever an approval is created.
- Payload: `{event, approval_id, session_id, kind, status, reason, created_at, updated_at}`
- Signature: `X-Webhook-Signature: sha256=<hmac>` (Slack-compatible)
- Secret: `APPROVAL_WEBHOOK_SECRET`

#### Auth Profile Export / Import
- `GET /auth-profiles/{name}/export` — downloads the named auth profile as a `.tar.gz` archive
- `POST /auth-profiles/import` — imports a `.tar.gz` archive into the auth root (supports `overwrite` flag)

#### Operator Dashboard
`/ui/` — dark-mode single-page operator dashboard served as static HTML.
- Session list with live status
- Screenshot panel with auto-refresh on SSE observe events
- SSE event log (newest first, capped at 200 entries)
- Pending approvals queue with one-click approve/reject
- Perception preset selector
- Screenshot diff button with pixel change readout

#### Python Client SDK
New `client/` package: `auto-browser-client` on PyPI (installable as `pip install auto-browser-client`).
- Sync and async variants for all core endpoints
- `stream_events()` generator for SSE
- `AutoBrowserError` with status code and detail

### Changed
- `GET /sessions/{id}/observe` now accepts optional `preset` query param (default: `normal`)
- `_observation_payload` returns a `preset` field indicating which mode was used
- `_page_summary` now accepts `text_limit` parameter (used by `rich` preset)
- Version bumped to `0.3.0` in FastAPI app and MCP transport

### Fixed
- `_write_tar` and `_compute_diff` are pure static methods — no BrowserManager instantiation needed for offline testing

## [0.2.0] — 2026-03-15

### Added
- 6 new REST endpoints: hover, select-option, wait, reload, go-back, go-forward
- ruff CI linting job
- 9 new unit tests
- `.env.example` improvements

## [0.1.0] — Initial release

- Playwright-based browser controller
- MCP JSON-RPC transport
- Agent step/run with OpenAI, Claude, Gemini
- Approval workflow (upload/post/payment/destructive)
- Auth profile management
- noVNC human takeover
- Docker isolation mode
- Social actions (post, comment, like, follow, dm)
- Audit log + metrics
