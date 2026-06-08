# Changelog

All notable changes to auto-browser are documented here.

## [Unreleased]

## [1.1.4] — 2026-06-08

### Changed
- Documented the single-writer chain-integrity invariant on `WitnessRecorder`: the `asyncio.Lock` and in-memory head-hash cache are correct only within one process (the supported single-worker uvicorn deployment), so running multiple workers against the same `witness_root` would fork the receipt hash chain. No functional changes.
- Bumped controller, client, LangChain integration, and browser-node package metadata to `1.1.4`, and refreshed release-facing version strings in the dashboard badge, webhook user-agent, README highlights, launch notes, and good-first-issue docs.

## [1.1.3] — 2026-06-02

### Fixed
- Fixed fire-and-forget background work (network-capture listeners, the on-detach pending flush, approval webhook dispatch, and post-session curator review) being scheduled without a retained task reference, which allowed CPython to garbage-collect the tasks before they completed. They now route through a shared `spawn_background_task` helper that holds a strong reference until the task finishes.
- Fixed operator-identity audit attribution sharing a single mutable `OperatorIdentity` instance as a `ContextVar` default across all request contexts. The default is now `None` and `get_current_operator()` mints a fresh anonymous identity when no operator is set.

### Removed
- Removed the extracted social (YouTube/Instagram/Reddit/X) clients and the Veo3/viral-research integration, along with their unit tests and the now-empty packaging exclude. They were already unwired from routes, tools, the orchestrator, and startup, and excluded from the wheel build; the guard tests that assert those routes/tools stay unshipped are retained.

### Changed
- Bumped controller, client, LangChain integration, and browser-node package metadata to `1.1.3`, and refreshed release-facing version strings in the dashboard badge, webhook user-agent, README highlights, launch notes, and good-first-issue docs.

## [1.1.2] — 2026-06-02

### Added
- Added regression coverage for Host-header path confusion so crafted Host values cannot bypass bearer-token checks.
- Added CI gates for Python dependency audits, browser-node npm audits, fixture eval validation, client tests, and Python wheel builds.
- Added Dependabot configuration and CI dependency-audit gates for recurring security coverage.
- Added concrete benchmark manifest tracking for WebArena-style, Online-Mind2Web-style, and CUAVerifier regression lanes.

### Changed
- Bumped controller, client, LangChain integration, and browser-node package metadata to `1.1.2`.
- Upgraded FastAPI to `0.136.3` and Starlette to `1.0.1`.
- Made `CONTROLLER_ALLOWED_HOSTS` a production startup requirement instead of a warning.
- Raised the controller CI coverage gate from 65% to the release-audit 80% threshold.
- Switched the browser-node Docker build to `npm ci` against the committed lockfile.

### Fixed
- Fixed bearer-token, rate-limit, operator-identity, and metrics middleware path handling to use ASGI scope paths instead of reconstructed URL paths.
- Fixed stale `v1.1.0` release-facing version strings in dashboard, webhook user-agent, README, launch notes, and good-first-issue docs.

## [1.1.1] — 2026-05-17

### Added
- Added `.github/FUNDING.yml` wiring GitHub Sponsors.
- Added `docs/session-isolation-audit.md` documenting per-session isolation across `shared_browser_node` and `docker_ephemeral` modes.

### Changed
- Renamed the regulated compliance template names to neutral policy presets: `HIPAA`/`PCI-DSS` → `strict`, `SOC2`/`GDPR` → `balanced`. Legacy names still work as deprecated aliases and emit a warning at startup.
- Promoted read-only harness inspection tools (`harness.list_runs`, `harness.get_status`, `harness.get_trace`) into the default `curated` MCP tool profile so agents can introspect harness state without elevated access. Convergence runs, drift checks, candidate management, and graduation still require `MCP_TOOL_PROFILE=full`.
- Restored convergence harness positioning in the README as a first-class feature and v1.1.0 release highlight.
- Simplified compliance-template normalization at controller startup; the preset module is now the single source of truth for validation and alias resolution.
- Updated `LICENSE` copyright holder to JAI Studios.
- Moved tip/sponsor pointers from the README body into `TIPS.md` and added the GitHub Sponsors link there.
- Bumped controller, client, LangChain integration, and browser-node package metadata to `1.1.1`.

### Removed
- Removed the unused `open` compliance preset that was added during the rename pass; only `strict` and `balanced` remain.

## [1.1.0] — 2026-05-09

### Added
- Added MCP resource listing and subscription surfaces for session observations, harness run status, staged candidates, and recent audit events.
- Added per-tool MCP metrics and response metadata for latency, status, and tool identity.
- Added skill drift monitoring with `harness.check_drift` and `harness.check_all_drifts` so staged skills can be re-verified after site or contract changes.
- Added `/healthz/deep` to exercise a real browser context against a deterministic fixture, with an embedded fallback when packaged without `evals/`.

### Changed
- Refactored the controller into an app factory, focused routers, controller middleware modules, tool packs, browser services, and a shared action pipeline while preserving the public API.
- Split BrowserManager internals into lifecycle, action execution, runtime policy, approvals, observation, auth profile, witness, human takeover, bot challenge, tabs, downloads, uploads, and TOTP services.
- Split the MCP tool gateway into a registry plus domain tool packs with MCP tool annotations for read-only, destructive, idempotent, and open-world behavior.
- Centralized SQLite WAL connection tuning for audit, approvals, traces, and related stores.
- Bumped controller, client, LangChain integration, and browser-node package metadata to `1.1.0`.

### Fixed
- Fixed tool descriptor caching so tools/list avoids repeated schema generation while returning JSON-safe copies.
- Fixed broad route-level failure handlers to log traceback context before returning stable `500` responses.
- Fixed staged skill drift checks so artifact reads are derived from the candidate directory instead of trusting recorded absolute paths.
- Fixed app startup so router registration failures fail closed instead of booting a partial controller.
- Fixed deep health packaging behavior so missing fixture files are visible in logs but do not break packaged deployments.

## [1.0.6] — 2026-05-09

### Added
- Added the Stage 0 convergence harness for Agent Skill Induction with task contracts, hash-chained traces, deterministic verification, budgeted iteration, staged skill induction, and CLI smoke support.
- Added Universal Verifier adapter boundaries and ensemble verifier plumbing so UV/Stagehand-style verification can be swapped in without changing harness callers.
- Added staged skill candidate artifacts with provenance, embedded self-tests, trace/contract copies, and optional mesh-signed envelopes that are round-trip verified before write.
- Added full-profile MCP tools for harness runs and staged candidate review: `harness.start_convergence`, `harness.get_status`, `harness.get_trace`, `harness.list_runs`, `harness.list_candidates`, `harness.get_candidate`, and `harness.graduate`.
- Added benchmark scaffolds for WebArena, Online-Mind2Web, and CUAVerifierBench plus a deterministic example contract.
- Added `/version` for operator/runtime identification.

### Changed
- Requires `workflow_profile=governed` for page-level JavaScript evaluation and live-session harness convergence.
- Tightened bearer-token exemptions so dashboard/UI roots are exact-match only, not broad path prefixes.
- Removed the unused external HTMX CDN script from the operator dashboard.
- Strengthened approval webhook target validation against loopback, private, link-local, metadata, unspecified, multicast, and reserved IP targets.
- Improved gateway, cron, harness run, and staged-candidate logging around failures and unreadable artifacts.
- Bumped controller, client, LangChain integration, and browser-node package metadata to `1.0.6`.

### Fixed
- Fixed staging path traversal and long-slug collision risks in induced skill IDs.
- Fixed trace hash-chain validation so tampering is detected on read, not only at write time.
- Fixed sensitive trace fields so auth, cookie, token, password, secret, and API-key payloads are redacted before persistence.
- Fixed harness startup failure behavior so MCP callers get a clear harness-unavailable error instead of a generic tool failure.

## [1.0.5] — 2026-05-07

### Added
- Added local HTML eval fixtures for auth-profile reuse, popup/download recovery, governed write blocking, upload approval, resume-after-failure, and multi-tab recovery.
- Added `scripts/fixture_eval.py` and `make fixture-eval` to validate release-critical browser workflow fixtures without a live provider.
- Added portable `scripts/release_audit.py` with fixture validation, dependency audits, wheel builds, controller wheel inspection, compile checks, and an 80% controller coverage gate.

### Changed
- Removed the social/Veo3 HTTP and MCP surface from the shipped controller package instead of keeping it behind an environment flag.
- Removed the legacy `EXPERIMENTAL_SOCIAL` configuration knob from the production posture.
- Raised controller coverage to the 80% release gate with focused BrowserManager, route, event, webhook, and session-store coverage.
- Updated public docs to describe Auto Browser as the authorized browser MCP surface only.

## [1.0.4] — 2026-05-06

### Added
- Added enforced governed workflow approvals for non-read agent actions, including generic click/type writes.
- Expanded the agent eval matrix to cover auth reuse, popup/download recovery, upload approval, resume recovery, multi-tab recovery, and `fast` versus `governed` divergence.
- Added deterministic mock eval mode via `make eval`.
- Added `EXPERIMENTAL_SOCIAL` to explicitly gate social/Veo3 HTTP routes, workflow actions, startup clients, and MCP tools.

### Changed
- Changed `STEALTH_ENABLED` to default to `false`.
- Hid social/Veo3 tooling from the default controller and MCP surface unless `EXPERIMENTAL_SOCIAL=true` and `MCP_TOOL_PROFILE=full`.
- Replaced stale public good-first-issue suggestions with current gaps around multi-tab recovery, MCP resources, auth-profile setup, approval fixtures, and replay.
- Moved crypto tip addresses out of the README and into `TIPS.md`.
- Strengthened `scripts/release_audit.sh` with Python dependency audit, browser-node npm audit, Python wheel builds, mock eval scoring, and a controller coverage ratchet gate.

## [1.0.3] — 2026-05-05

### Added
- Added request-level agent workflow profiles: `fast` for direct execution and `governed` for conservative review guidance.
- Added durable per-step checkpoints for background agent jobs plus REST/MCP resume support for interrupted, failed, or step-limited runs.
- Added dashboard controls for viewing, resuming, and discarding background agent jobs with checkpoint timelines.
- Added a repeatable agent eval harness for provider/profile comparison in offline scoring or live-controller execution mode.
- Added explicit REST/MCP/dashboard cancellation for queued and running background agent jobs.

### Changed
- Split session artifact preparation, screenshots, trace payloads, and JSONL persistence into a dedicated artifact service.
- Split download capture persistence into a dedicated service and added CI validation for eval case files.
- Added a committed browser-node package lock so npm audit produces a real release gate.

## [1.0.2] — 2026-04-26

### Fixed
- Upgraded FastAPI and Starlette to pick up the latest framework security fixes
- Replaced remaining client-visible raw exception strings with stable error codes across CDP, OCR, mesh, workflow, tunnel, social, share-token, and browser action paths
- Bounded request rate-limit buckets and hashed operator identifiers so untrusted headers cannot expand memory or echo raw identity values
- Tightened default host allowlisting and production runtime policy checks for `ALLOWED_HOSTS`
- Strengthened auth/upload path containment checks for absolute paths and traversal edge cases

## [1.0.1] — 2026-04-25

### Added
- Added regression coverage for Playwright session export script generation

### Fixed
- Reapplied the closed CodeQL hardening fixes to the release line for workflow permissions, path validation, URL allowlist checks, reflected XSS, and stack-trace exposure
- Bound remaining route exceptions to fixed error responses so unexpected manager failures do not expose internals
- Corrected reusable auth profile export/import so archives round-trip through `AUTH_ROOT/profiles`
- Restored Python client package builds and updated the SDK to the current action and audit REST routes
- Rebuilt the operator dashboard tables with DOM text nodes and validated links instead of interpolating untrusted values into `innerHTML`
- Fixed the active-session dashboard stat update
- Updated the bundled operator UI version label to `v1.0.1`
- Aligned package metadata, launch notes, roadmap text, and stale code comments with the `v1.0.1` release line
- Redirected legacy `/ui/` requests to `/dashboard` so secured deployments consistently land on the bootstrap-aware operator dashboard

## [1.0.0] — 2026-04-21

### Added
- Signed mesh envelopes, peer registry routes, and delegation plumbing for trusted node-to-node work distribution
- Session network inspection, CDP passthrough, workflow routes, social route surface, and the bootstrap-aware `/dashboard`
- Curator, Veo3/research, and social client packages merged into the controller tree for the 1.0 release line

### Fixed
- Mesh recipient validation so signed envelopes cannot be replayed to the wrong node
- False-success delegation responses when tool/workflow/session handlers fail or require approval
- Session network and CDP wiring so session lifecycle hooks register inspectors and passthrough state correctly
- Windows agent-job persistence, audit retention ordering, and tar extraction safety in the host test path
- Legacy `/ui/` routing and operator-auth bootstrap handling so secured deployments land on the current dashboard

## [0.7.0] — 2026-04-17

### Added
- Deployment readiness advisor (`GET /readiness`, `browser.readiness_check` MCP tool)
- Compliance templates: HIPAA, SOC2, GDPR, PCI-DSS via `COMPLIANCE_TEMPLATE` env var
- Compliance manifest written to `/data/compliance-manifest.json` on startup
- GitHub Codespaces devcontainer for one-click live demos without local Docker
- Agent memory profiles (`browser.save_memory_profile`, `browser.get_memory_profile`, `browser.list_memory_profiles`, `browser.delete_memory_profile`)
- Memory profile context injected into the orchestrator prompt prefix
- LangChain / LangGraph / CrewAI integration package under `integrations/langchain/`
- `LOG_LEVEL` environment variable for runtime log level control
- `browser.find_by_vision` is now absent from the tool list when `ANTHROPIC_API_KEY` is not configured

### Fixed
- Bearer token comparison now uses constant-time `hmac.compare_digest`
- `storage_type` is validated to `{"local", "session"}` to harden storage access
- `generic_hex_token` PII matching is now opt-in by default to reduce false positives
- `phone_us` PII matching no longer trips on version-string-like inputs
- Vision targeting now defaults to `VISION_MODEL=claude-haiku-4-5-20251001`
- `ALLOWED_HOSTS` now defaults to `"*"` for frictionless local development
- MCP session persistence now evicts excess sessions to avoid unbounded growth
- SQLite-backed approval and audit stores now close connections correctly during host-side test runs

## [0.5.4] — 2026-04-16

### Fixed

#### Dependency security updates
- bumped Python `cryptography` from `46.0.5` to `46.0.6` to clear `GHSA-m959-cc7f-wv43` / `CVE-2026-34073`
- bumped controller and browser-node `playwright` from `1.52.0` to `1.56.0` to clear `GHSA-7mvr-c777-76hp` / `CVE-2025-59288`

### Added

#### Hosted Witness forwarding from the controller
`auto-browser` can now forward local Witness receipts into a hosted Witness deployment.

The controller now supports:

- `WITNESS_REMOTE_URL`, `WITNESS_REMOTE_API_KEY`, and `WITNESS_REMOTE_TENANT_ID`
- per-session remote delivery status in session summaries and persisted session records
- hosted Witness preflight for confidential sessions when `WITNESS_REMOTE_REQUIRED_FOR_CONFIDENTIAL=true`
- fail-open remote delivery for normal sessions so browser work keeps moving even if the hosted Witness service is degraded

### Changed

#### Confidential Witness delivery behavior
Confidential sessions can now require a reachable hosted Witness service before mutating
actions or auth-material saves run. This keeps strict deployments from discovering
after-the-fact that the external system of record was unavailable.

Session creation remains local-first so operators can still establish and inspect
confidential sessions while strict hosted delivery gates apply to write/auth work.

## [0.5.3] — 2026-04-01

### Added

#### Witness receipts and protection profiles
Added a first-pass `Witness` core inside the controller with two protection modes:

- `normal` — records tamper-evident, hash-chained action receipts without adding new
  user-facing constraints
- `confidential` — adds stricter policy checks for high-risk actions, stronger evidence
  restriction, and blocks unsafe auth-material handling when encryption/isolation posture
  is too weak

The controller now:

- persists per-session Witness receipts under a dedicated witness store
- attaches Witness receipt recording to session lifecycle events, browser actions,
  human takeovers, and auth-state/profile saves
- exposes `protection_mode` on session creation and session summaries
- exposes `GET /sessions/{id}/witness` for receipt inspection

Runtime policy now also warns when confidential mode is the default but the deployment
is still using weak isolation or unencrypted auth-state settings.

### Fixed

#### Witness packaging and runtime hygiene
- Added `WITNESS_ROOT`, `WITNESS_ENABLED`, and `WITNESS_PROTECTION_MODE_DEFAULT` to the documented environment surface
- Added `data/witness/.gitkeep` and ignore rules so runtime receipts do not dirty the repo during local smoke runs
- Extended HTTP and controller tests to cover the witness route, approval lifecycle recording, and confidential auth-material blocking

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
