# Changelog

All notable changes to auto-browser are documented here.

## [Unreleased]

### Security

- **The API is no longer unauthenticated by default when it is reachable.**
  Three layers were supposed to answer "is this API authenticated?" and all
  three failed open. `API_BEARER_TOKEN` defaulted to unset; the bearer
  middleware returned early when it was unset, so the *absence* of a credential
  disabled authentication instead of denying requests; and the only hard check
  lived in startup validation, which downgraded to a warning unless
  `APP_ENV=production` — while shipped compose set
  `APP_ENV: ${APP_ENV:-development}`. The default was an open control plane
  driving a browser that holds stored logins.

  The switch is now reachability rather than an environment name, because
  reachability is what decides whether an open control plane is a local
  convenience or an account takeover. New setting **`API_BIND_SCOPE`**
  (`loopback` | `exposed`) declares it, and the controller refuses to start when
  the scope is `exposed` without a token of at least 32 characters — in any
  `APP_ENV`.

  It defaults to **`exposed`**, so a deployment that declares nothing is assumed
  reachable and fails closed. The base `docker-compose.yml` declares `loopback`
  to match its `127.0.0.1:8000:8000` publish mapping, so `docker compose up` on
  a clean checkout still needs no token; the Codespaces overlay declares
  `exposed`. A hand-rolled compose file that publishes on `0.0.0.0` without
  saying so now refuses to start instead of silently serving an open API. If you
  run the controller directly, a loopback bind host in `API_BIND_HOST`,
  `UVICORN_HOST` or `HOST` is detected and treated as `loopback`.

  Authentication is decided in one place (`app/auth_policy.py`) for both the
  auth gate and the rate limiter, and the request path is what the tests assert:
  a reachable, tokenless controller answers 401 to `/sessions` while `/healthz`
  stays reachable for orchestrators.

  **Upgrading:** if you publish the API anywhere other than loopback and were
  relying on it being open, set `API_BEARER_TOKEN` (32+ characters). If you
  publish only on loopback with a compose file of your own, add
  `API_BIND_SCOPE=loopback`.

- **Auth profiles now belong to the operator who saved them.** Profiles lived in
  a flat root with no owner, so any caller who could reach the API could read
  one, export its cookie archive, or — the takeover that matters — open a
  session against it and drive a browser already logged in as somebody else.

  A profile saved by a caller with a *proven* identity records that operator as
  its owner. Reading, exporting, overwriting on import, saving over it, and
  opening a session from it then require authenticating as that operator, and
  listing hides profiles you cannot access rather than advertising which sites
  someone else holds logins for. Refusals are `403`, including on export, where
  the route's catch-all previously turned any refusal into a `500`.

  Ownership keys on `source: "token"`, so *claiming* to be the owner in an
  `X-Operator-Id` header does not work. It also means ownership starts applying
  exactly when named credentials do: a profile saved without a proven identity
  records no owner, so shared-token deployments and every profile written before
  this release keep working, with no migration step and no way to be locked out
  of your own logins.

- **Operator identity can now be proven instead of asserted.** `X-Operator-Id`
  is a request header, and until now it was the only thing that ever set the
  operator on an audit event — so the trail attributed actions to a string the
  caller chose, which reads as identity while being a label.

  New setting **`API_BEARER_TOKENS`** takes named credentials
  (`alice:token-a,bob:token-b`). A request authenticating with one of those has
  a verified identity recorded as `source: "token"`, and a `X-Operator-Id`
  header that disagrees no longer wins — the credential does, and the false
  claim is kept on the audit record as `asserted_id` rather than dropped. A
  named credential also satisfies `REQUIRE_OPERATOR_ID` on its own.

  The shared `API_BEARER_TOKEN` still works and still records
  `source: "header"`, because one shared credential genuinely cannot tell
  operators apart. That distinction is now explicit in the data, so
  authorization can require `source: "token"` — which is what the next release
  needs in order to scope auth profiles to an owner.

  Every named token must also clear the 32-character floor; one weak entry in
  `API_BEARER_TOKENS` is a way in.

- **A privately reported advisory can no longer sit unnoticed.**
  [GHSA-xmh3-cw7j-9gp5](https://github.com/LvcidPsyche/auto-browser/security/advisories/GHSA-xmh3-cw7j-9gp5)
  was reported on 2026-06-17 and sat in triage for seven weeks, because a
  private report appears in none of the views a maintainer opens day to day —
  not issues, not pull requests — and was found only by listing advisories
  through the API while publishing an unrelated one. `SECURITY.md` promises
  reporters a quick acknowledgement.

  `scripts/check_open_advisories.py` now fails the release audit while any
  report is still waiting in triage. Accepted drafts are reported but do not
  block, since accepted-and-being-fixed is a legitimate place to be mid-release.
  The check attaches to cutting a release because that is the one process
  guaranteed to run before users are affected by anything.

  The blocking path is what the tests cover: a gate exercised only against a
  clean repo is indistinguishable from one that always reports "all clear".

### Notes

The design items listed as *Known and not yet addressed* under 1.6.1 are the
scope of 1.7.0, along with one more from the same report that the 1.6.1 list
omitted: skill-candidate provenance is hashed but never signed or verified.

## [1.6.1] — 2026-08-04

Security fixes from a **privately reported** vulnerability disclosure (received
2026-06-17). Reported findings, verified before fixing.

**If you use the Codespaces overlay, upgrade and set `API_BEARER_TOKEN`.**

### Security

- **`Runtime.evaluate` and `Network.getCookies` were in the "safe" raw-CDP
  allowlist.** `/sessions/{id}/cdp/raw` is gated solely by
  `_ALLOWED_CDP_COMMANDS`, and that list — described as safe — permitted
  arbitrary JavaScript execution in the page context. Because sessions reuse
  stored auth profiles, that meant cookie theft and acting as the logged-in user
  on every site a profile is authenticated to, plus `fetch()`-based SSRF to
  internal and cloud-metadata addresses which bypasses the navigation allowlist
  entirely. `Network.getCookies` returned credential material directly.

  Both removed. This does not affect internal element intelligence (which issues
  CDP commands directly rather than through `raw_cdp_command`) or
  `browser.eval_js` (which uses Playwright's `page.evaluate` and is governed and
  auditable). The allowlist is now pinned by tests that reject any entry which
  is not read-only introspection.

- **The Codespaces overlay published an unauthenticated control plane.**
  `docker-compose.codespaces.yml` binds the API to `0.0.0.0` so the port
  forwarder can reach it, while `API_BEARER_TOKEN` defaults to unset and the
  bearer middleware fails open when it is. Combined with the CDP issue above,
  anyone who could reach the forwarded port could open a session from a saved
  auth profile and export its cookies.

  The overlay now requires `API_BEARER_TOKEN` via Compose's `${VAR:?message}`
  form, so it refuses to start rather than silently publishing an
  unauthenticated browser control plane.

- **Raw VNC is no longer published on `0.0.0.0` in that overlay.** `x11vnc` runs
  `-nopw`, so port 5900 is unauthenticated by design and is only safe behind a
  loopback bind. noVNC (6080) remains published, so human takeover still works.

### Known and not yet addressed

The same report raised items that need design decisions rather than patches, and
they are deliberately **not** claimed as fixed here: the API remaining
unauthenticated by default outside the Codespaces overlay (loopback-bound in the
base compose), auth profiles not being owner-scoped, operator identity being a
self-asserted header, and the Codex host-bridge running with sandbox approvals
bypassed. These are tracked for the next release.

## [1.6.0] — 2026-08-04

Witness receipts are now signed, and an exported bundle can be verified by
someone who does not run — and does not trust — this controller.

This also closes the August execution audit. Its full findings, reproductions,
and the gates that close the defect class are published in
[`docs/audits/2026-08-execution-audit.md`](./docs/audits/2026-08-execution-audit.md),
and the two findings with user-facing security impact are disclosed as
[GHSA-32ph-8hp6-7qgj](https://github.com/LvcidPsyche/auto-browser/security/advisories/GHSA-32ph-8hp6-7qgj).

### Added

- **Ed25519-signed Witness receipt chains.** The chain was SHA-256 linked and
  nothing else, which detects accidental corruption and nothing more: anyone
  holding the JSONL could edit a receipt, recompute every downstream hash, and
  produce a chain that `verify()` passed. Each receipt's `chain_hash` is now
  signed, and because that hash already covers the entire preceding chain, one
  signature attests to both the receipt and its history to that point. Signing
  is on by default — keys live at `<witness_root>/keys` and generate on first
  use.
- **Exportable evidence bundles.** `GET /sessions/{id}/witness/bundle` and
  `browser.export_witness_bundle` produce a self-contained bundle: every
  receipt, the head hash, the signing key id, and the public key.
  `scripts/verify_witness_bundle.py` checks it while importing nothing from this
  project — that independence is what makes a receipt evidence rather than an
  assertion about itself.
- **`verify_witness_chain` now returns a `signatures` block**, so `valid: true`
  can never be mistaken for "attested". The hash chain only ever proved internal
  consistency.
- **Auth-profile export and import emit Witness receipts.** They previously had
  none, because receipts were session-scoped and these operations have no
  session; they now record under an `auth-profiles` scope with their own signed
  chain. A witness outage logs and continues — recording evidence must never
  fail the operation it records.

### Fixed

- `WitnessRecorder.list(limit=0)` sliced `lines[-0:]` — the whole list — so a
  caller passing a computed limit that reached zero received the entire receipt
  chain instead of an empty page.

### Notes

Existing chains keep verifying unchanged: the signature fields are excluded from
the hash input, so it stays byte-identical to what pre-signing builds produced.

Two limits are stated in `witness_signing.py` rather than left to assumption.
**Tail truncation** remains undetectable without an external anchor — dropping
the last k receipts leaves a shorter chain whose signatures all still verify, so
`verify()` reports the head for comparison against an independently obtained
one. **Key compromise** still allows rewriting history; signing raises the bar
from "anyone with the file" to "anyone with the key", and is not tamper-proof
storage.

## [1.5.3] — 2026-08-04

Closes the audit. The remaining lead turned out to hide a worse defect than the
one it described.

**If you set `REQUIRE_AUTH_STATE_ENCRYPTION=true`, upgrade.** It did not do what
it says.

### Security

- **`REQUIRE_AUTH_STATE_ENCRYPTION=true` loaded plaintext auth state.** The
  setting was enforced only at construction — "is a key configured?" — and never
  on the read path, so a plaintext state file loaded happily and its cookies went
  straight into a live browser context while the deployment believed encryption
  was mandatory. Measured against the real code:

  ```
  inspect().encrypted  : False
  encryption_required  : True
  prepare_for_context  : ACCEPTED, cookie value = 'SUPER-SECRET'
  ```

  `prepare_for_context` now refuses unencrypted state when encryption is
  required (#137).
- **Auth-state encryption was classified by filename, not content.** `inspect()`
  decided `encrypted` from `path.name.endswith(".enc")`, so the classification
  was wrong in both directions. An encrypted envelope named without `.enc` was
  *not* leaked as plaintext — it is ciphertext — but it was handed to Playwright
  verbatim as if the envelope were storage state, so no cookies loaded and the
  agent proceeded believing the auth profile had been applied. A silent auth
  failure rather than an exposure. Content now decides for a file that exists;
  the suffix remains only as the hint for a path that does not exist yet, and an
  unclassifiable file falls back rather than guessing (#137).

### Fixed

- **`/readyz` could hang for a minute while holding the global browser lock.**
  It called `ensure_browser()`, which retries `CONNECT_RETRIES` (default 60)
  times a second apart while holding `_browser_lock`, so with browser-node down
  every concurrent probe and every `create_session` queued behind it — the API
  looked dead rather than reporting "not ready", which is the opposite of a
  readiness probe's purpose. Now bounded at 5 seconds (#138).
- **Isolated sessions leaked their entire profile tree.** `_release_sync` removed
  the container but never `<data>/browser-sessions/<id>`, which holds a full
  Chromium profile — tens to hundreds of megabytes per session — and the
  maintenance sweep covers only the artifact, upload and auth roots. Unbounded
  growth with no signal until the volume filled mid-session. Removal is refused
  for any path resolving outside the managed runtime root (#138).
- **browser-node's healthcheck watched the wrong service.** It probed noVNC
  `:6080`, not the Playwright server `:9223` the controller actually depends on,
  so a wedged or dead Chromium reported healthy for as long as websockify
  survived — and the controller's `depends_on: service_healthy` gate passed
  anyway (#137).
- **No restart policy on controller or browser-node**, so a controller OOM left
  the container stopped forever with nothing recovering. Added, with a 60s
  `stop_grace_period`: sessions close sequentially and an isolated one can take
  ~10s, so the 10s default SIGKILLed cleanup mid-flight and orphaned containers
  and tunnels (#137).

### Changed

- `controller/pyproject.toml` raised to `requires-python = ">=3.11"` (with mypy
  and ruff `target-version` to match) — the last instance of the unbacked-floor
  pattern. Its ruff target also disagreed with the root `ruff.toml` (py310 vs
  py311), which meant the two byte-identical stdio-bridge copies were formatted
  under different targets and could have drifted apart silently (#137).

### CI

- **The Python floor is now an invariant, not a repeated correction.** This
  pattern shipped two bugs — 3.14 advertised and untested in langchain, 3.10 in
  the client and mcp packages. A test parses the CI matrix and fails if any
  package declares a Python version CI does not run, or ships a classifier below
  its own floor (#137).

## [1.5.2] — 2026-08-04

The rest of the audit that produced 1.5.1. Same theme — guarantees that were
never verified end to end — but these fail by *leaking*, *wedging*, or *going
quiet* rather than by silently permitting.

**If you use cron triggers, upgrade.** The first cron fire permanently consumed
your only session slot.

### Fixed

- **Cron-triggered sessions were never closed, and the first fire wedged the
  service.** `_run_job_now` created a session per fire and nothing ever released
  it. With `MAX_SESSIONS` defaulting to **1**, the first cron fire consumed the
  only slot permanently — every later fire *and every manual `create_session`*
  then failed "Session limit reached". Under `docker_ephemeral` each fire also
  leaked a browser container. The job queue now carries a finish-callback
  registry invoked from a `finally`, so the session is released on completion,
  failure and cancellation alike; a fire is skipped while that job's previous
  run is still in flight; and a rejected enqueue releases too (#132).
- **The maintenance cleanup loop died permanently on its first exception**, with
  nothing observing it — artifacts, uploads and auth then grew forever with only
  a stale `last_report` as the clue. A live trigger sat in the same file: the
  `st_size` stat raced concurrent deletion while the `mtime` stat above it was
  already guarded (#132).
- **A corrupt cron store erased every cron job.** `_load` returned `{}` on a
  parse error and the next `_save` wrote that empty dict over the damaged file.
  It now quarantines the file and raises (#132).
- **One torn line broke audit listing forever.** A kill mid-append leaves a
  partial record; validation raised, so `list` 500s'd permanently and `_trim_sync`
  could never age the bad line out (#132).
- **Agent-job and orchestrator failures discarded the real exception.** Jobs
  recorded the constant `"agent_job_failed"` and logged nothing; `orchestrator.py`
  did not even import `logging`. A crashing provider adapter, a Playwright error
  and a JSON parse failure were indistinguishable, with no traceback anywhere
  (#132).
- **The published client raised the wrong exception on a failed SSE stream.**
  `_raise` read `r.text` on a streaming response, which raises
  `httpx.ResponseNotRead` — so a 401 on `stream_events` surfaced as an httpx
  internal error instead of `AutoBrowserError(401, ...)` (#133).
- **`.get(key, default)` defeated by explicit null / empty list in the LangChain
  package.** `{"content": []}` gave `IndexError`; `{"content": null}` gave
  `TypeError`, inside a LangChain tool call. Same class as the crash that shipped
  in v1.4.2 (#133).
- **Fire-and-forget task failures** surfaced only as asyncio's GC-time "exception
  was never retrieved" warning, and **SSE drops were DEBUG-only**, so an operator
  watching a live stream silently missed actions (#132).

### Security

- **Rate limiting could not throttle bearer-token brute force.** The bucket key
  was derived from the `authorization` header, so every guessed token hashed to
  its own fresh bucket — an attacker never shared a counter with themselves and
  never hit the limit. An attacker-chosen `X-Operator-Id` did the same, and a
  flood of distinct values could evict legitimate buckets from the LRU.
  Unauthenticated requests are now keyed by client IP (#134).
- **Auth-profile export and import left no audit record.** `save_storage_state`,
  which merely *writes* auth material, is wrapped in witness policy, an audit
  event and a receipt. Export — which packages every cookie and localStorage
  entry for a logged-in account into a downloadable archive — and import had
  none of it (#135).
- **Auth-profile archive import had no limits** on member count, member size, or
  total expanded bytes, so a decompression bomb dropped in `AUTH_ROOT` could
  exhaust the disk (#134).
- **Production hardening had two holes.** `API_BEARER_TOKEN=x` satisfied
  "required" — there is now a 32-character floor. And `SHARE_TOKEN_SECRET` was
  unchecked: with no flag to disable sharing the endpoint is always reachable,
  and without a configured secret the signing key is generated per process, so
  every share link silently died on restart (#134).
- **redis had no socket timeouts**, so a hung (not refused) server blocked
  `upsert` indefinitely — and `upsert` runs inside `create_session` and after
  every successful action (#132).
- **Unbounded Prometheus label cardinality**: the raw URL path became a metric
  label, so on a public MCP endpoint ordinary scanner traffic grew the registry
  for the lifetime of the process (#132).

### Changed

- **`requires-python` raised to `>=3.11`** for `auto-browser-client` and
  `auto-browser-mcp`, and the 3.10 classifier dropped. Both declared 3.10 support
  that CI never tested — the same unbacked-claim pattern that shipped the v1.4.2
  langchain bug (#133).
- **`docker-compose.yml` pinned model defaults `config.py` had moved past**
  (`gpt-4.1-mini`, `claude-sonnet-4-20250514`, `gemini-2.5-flash`). Docker is the
  primary deploy path, so those users silently ran older models than pip installs
  and than anything CI tested — a delayed failure surfacing as provider 404s when
  an old model id retires. Synced and pinned by a test (#135).
- Added `.gitattributes`; line endings previously depended on each contributor's
  git config, on a repo whose `browser-node/Dockerfile` already carries a
  `sed -i 's/\r$//'` workaround for that exact class (#133).

### CI

- **Security guards with zero coverage are now pinned.** Every rejection branch
  in `require_approved` and in share-token validation never executed under the
  suite, so a refactor weakening one would have shipped green. Covered: approval
  from another session, of another kind, and for a **mutated action** (approve a
  click on element A, execute on element B); rejected and unapproved approvals;
  double execution; share-token tampering, foreign-secret signing, and expiry.
  None of these found a bug — they exist so a future change cannot quietly
  remove the guard (#134).
- Compose-vs-config model parity is enforced by a test, joining the existing
  version-string and Playwright-pin guards (#135).

## [1.5.1] — 2026-08-04

Found by an adversarial audit of this repo, not by a user report. Several safety
controls were asserted in code and documentation but never verified end to end,
and three of them silently did nothing while reporting success. Every one was
green in CI.

**If you rely on screenshot PII redaction, upgrade.** The severity is bounded by
auto-browser being local-first and single-tenant — for most operators the
affected artifacts never left their own box — but the real exposure surfaces are
shared session links, auth-profile exports, and remote witness sinks.

### Security

- **Screenshot PII redaction was a silent no-op.** `OCRExtractor` emits geometry
  nested under `block["bbox"]`; `scrub_screenshot` read flat `x`/`y`/`width`/
  `height`, so every `.get(..., 0)` fell to its default and every redaction
  rectangle computed to `(0, 0, 0, 0)`. Measured on a rendered image: **307 ink
  pixels in the secret region before, 307 after**, while the function returned a
  hit and the caller wrote a `pii_redaction / ok` audit event over an unredacted
  screenshot — which was then stored under `/data/artifacts/`, served over
  `/artifacts/`, and handed to the model. The existing test fed flat keys, a
  shape production never produces, which is why nothing caught it. A degenerate
  box is now refused rather than counted as a redaction (#129).
- **A typo in `PII_SCRUB_PATTERNS` disabled all PII scrubbing, fail-open.**
  Unknown pattern names were "silently dropped" by intersecting with the known
  set, so `PII_SCRUB_PATTERNS="emial,phon"` produced an empty set — and an empty
  but non-`None` set makes `scrub_text` skip every pattern. `summary()` still
  reported `enabled: True`. Unknown names now raise at construction (#129).
- **The AWS access-key pattern could never match a real key.** It spelled the
  prefix as `A[KSIARP][IDA]` (3 chars) + 16 = 19 characters; real AWS key ids are
  20, so the trailing lookahead rejected every one. `AKIAIOSFODNN7EXAMPLE` passed
  through unredacted. The eight documented prefixes are now explicit (#129).
- **Most screenshots were never redacted at all.** `PII_SCRUB_SCREENSHOT=true`
  only covered `normal`/`rich` observes. The `fast` preset, manual captures, and
  the before/after snapshot taken on **every action** wrote unredacted PNGs. All
  screenshot writes now go through one redacting path (#129).
- **Redaction was starved by a token budget.** `image_to_data` returns one block
  per *word*, and the list was truncated at `ocr_max_blocks` (default 20), so
  only the first ~20 words of a page could ever be redacted — a navigation bar
  exhausted the budget before the content. OCR now returns an uncapped
  `redaction_blocks` alongside the capped model-facing `blocks`, and
  low-confidence blocks stay redaction-eligible because dropping them is
  fail-open for privacy (#129).
- **`cryptography` 49.0.0 → 50.0.0 for CVE-2026-69247.** [GHSA-g6cj-pr64-35w5](https://github.com/advisories/GHSA-g6cj-pr64-35w5) is a Bleichenbacher oracle in PKCS#7 `EnvelopedData` decryption: `pkcs7_decrypt_der`/`_pem`/`_smime` distinguished invalid RSA padding from a wrong key length (leaking the recovered length) from bad content padding, by both error and timing. **Not reachable in this controller** — `cryptography` is used here only for `Fernet` (auth state at rest) and `Ed25519` (mesh identity), neither of which is on the affected path. The bump clears a real CVE off a pinned runtime dependency rather than closing a live hole here, and it unblocked `dependency-audit`, which had gone red on every open PR the day the advisory published (#120).

### Changed
- **Playwright upgraded to 1.62.0 on both the pip and npm sides together** (#122), and Dependabot no longer proposes it from either ecosystem (#124). Playwright's websocket protocol requires an exact client/server version match, but the two pins are separate ecosystems to Dependabot, so it can only ever open half the change — which is not a smaller upgrade but the #60 outage, where every `docker compose up --build` crash-looped on readiness. It re-proposed that half in #97 (npm) and #121 (pip); `scripts/check_playwright_pins.py` blocked both. The guard stays and Playwright is now bumped by hand on both sides at once. 1.62 also raises the engine floor to Node ≥ 20, which `browser-node` meets because `python:3.11-slim` now aliases Debian trixie (Node 20.19).
- Dependency bumps: `fastapi` `>=0.141.1,<0.142` (#115), `uvicorn[standard]` 0.52.0, `redis` 8.1.0, `prometheus-client` 0.26.0, `ruff` 0.16.1 (#124).

### Fixed
- **`browser.export_script` had never worked over MCP.** `tool_gateway/gateway.py`
  did `from .playwright_export import ...`, which resolves to
  `app.tool_gateway.playwright_export` — a module that does not exist. The real
  module is `app.playwright_export`, and the REST route imported it correctly,
  which hid the difference. Every MCP call raised `ModuleNotFoundError` and the
  catch-all collapsed it into the opaque "Tool execution failed". No test touched
  the tool (#130).
- **The stealth init script was never valid JavaScript.** Two shell-escape
  artifacts (`\!`) inside an r-string reached the JS source, so
  `add_init_script` installed a script that threw at parse time in every page
  context — no stealth patch had ever applied, silently and page-side. Stealth
  is default-off and an explicit non-goal, so impact is low; the defect class is
  the point (#130).
- **MCP version negotiation violated a spec MUST.** `initialize` returned
  `-32602` for any unrecognised `protocolVersion`. The spec requires responding
  with a version the server *does* support so the client can downgrade, so a
  newer client probing for backwards compatibility got a dead connection instead
  of the graceful path (#130).
- **`-32002` meant two different things** — the spec's "resource not found" and
  "initialization required" — so clients could not tell them apart.
  Initialization-required moves to `-32005` (#130).
- **`browser.get_html` advertised a parameter that does nothing.** `full_page`
  was in the published schema and the description promised "visible viewport
  only", but the handler never read it and `page.content()` always returns the
  full DOM. Still accepted so callers passing it do not start getting 422s;
  marked deprecated and slated for removal in 1.6.0 (#130).

### CI
- **The Docker job ran 566 of 637 tests.** `controller-tests` used
  `unittest discover`, which only collects `unittest.TestCase` subclasses — so
  `test_1_0.py` (61 tests: mesh Ed25519 identity and signing, peer registry,
  delegation policy, stealth, network inspector, CDP, workflow engine),
  `test_pii_scrub.py` (8) and `test_readiness.py` (4) were invisible to it. The
  one required check that validates the shipped container ran zero PII-scrubber
  and zero mesh-crypto tests, and any new pytest-style file would have been
  dropped from it with no warning. Now runs pytest (#128).
- **Two gates for the defect class behind this release**, both confirmed to fail
  against the pre-fix code. `test_tool_surface_walk.py` resolves every relative
  import in all 129 modules under `app/`, at any depth, module-level or lazy —
  function-body imports are invisible to import-time checks and to linters that
  only resolve top-level imports, which is exactly why `export_script` survived.
  `test_browser_scripts_syntax.py` parses all 15 shipped `*_SCRIPT` constants
  with `node --check`; those strings only ever execute in a browser, so Python
  tooling could never validate them (#130).
- **All 16 PII patterns now have a behavioural test**, plus a completeness test
  that fails if a pattern is added without one. Twelve had no assertion at all,
  which is how the AWS defect survived. Redaction tests now assert pixels
  changed, driven through the real OCR code path so the two modules cannot drift
  apart again (#129).

### Added
- **`session_id` may be omitted on MCP tools.** With exactly one live session, tools target it; with none live, observe/act tools (`observe`, `execute_action`, `find_elements`, `screenshot`, `get_html`, `wait_for_selector`) create one on demand — an agent's first `browser.observe` now works with zero setup calls. Anything ambiguous (multiple live sessions, or a non-create tool with none) stays an explicit structured error (`ambiguous_session` / `no_session`) rather than a guess. Explicit `session_id` behavior is unchanged.

## [1.5.0] — 2026-07-30

### Added
- **`text` observation preset — read a page without paying for pixels.** `browser.observe` (and `GET/POST /sessions/{id}/observe`) now takes `preset="text"`: populated accessibility outline, text excerpt, DOM outline, and interactables with **no screenshot and no OCR**. This is the cheapest way for an agent to read a page's content, and closes the cost gap with accessibility-snapshot-first browser MCP servers. `screenshot_path`/`screenshot_url` stay in the payload as `null`, so consumers keep a stable shape.
- **`browser.find_elements` query mode.** Pass `query` (with optional `regex` and `context`) instead of `selector` to search the page's text content and get back each match plus surrounding text — locate one string on a page in a single cheap call instead of a full observe. Matching is case-insensitive in both modes; exactly one of `selector`/`query` is required.
- **OCR skipping on `normal`/`rich` observes** when text extraction already produced usable content (`OCR_SKIP_WHEN_TEXT_AVAILABLE`, default on). Never skipped while screenshot PII scrubbing is active — scrubbing consumes OCR's bounding boxes, so redaction always wins over token savings. Under default config (scrubbing on) OCR therefore still runs; use `preset="text"` for the real savings.

### Fixed
- **The MCP stdio bridge's cold-start error is actionable.** A first-time `uvx auto-browser-mcp` user with no controller running used to get `Unable to reach Auto Browser MCP HTTP endpoint: [WinError 10061]` — the exact moment an agent silently falls back to a competing server. The error now names the endpoint it tried and the fix (`docker compose up -d`, or `--base-url`/`AUTO_BROWSER_BASE_URL` for remote deployments).
- **Tool errors an agent can act on.** Invalid tool arguments now report pydantic's field-level errors, handler-raised `ValueError`/`KeyError`/`RuntimeError` messages ("Provide source_selector or source_x/source_y", "Memory profile not found") pass through to the caller, and an invalid regex in `find_elements` returns a structured `invalid_regex` error — all of these previously collapsed into an opaque `Tool execution failed`.
- **`PERCEPTION_PRESET_DEFAULT` actually works.** The setting existed but every observe path hardcoded `normal`, so it was dead config. An omitted preset now resolves against it (unknown values clamp to `normal` with a warning), and the client SDK stopped forcing `preset="normal"` on every call — omitted means the deployment default. One env var now flips a whole deployment to screenshot-free observes.
- **`./scripts/test_local.sh` no longer reports phantom failures on developer boxes.** It ran discovery from the repo root, where pydantic-settings picks up a developer's local dotenv — auth and rate limits turned on and ~138 route tests failed with 400s that CI and Docker (which have no dotenv) never see. Discovery now runs from `controller/`. The interpreter search also probes for the controller's dependencies rather than accepting on version alone, and falls back to the Windows `py` launcher — a stock Windows Git Bash box now finds its working interpreter without `AUTO_BROWSER_PYTHON_BIN`.

### CI
- **The stdio bridge's two copies are parity-enforced.** `client/auto_browser_client/mcp_bridge.py` and `controller/app/mcp_stdio.py` are intentionally byte-identical, and nothing caught an edit applied to only one. `scripts/check_bridge_parity.py` now fails CI on drift — same one-thing-two-sources class as the version and Playwright pin guards.

## [1.4.2] — 2026-07-25

### Fixed
- **`AutoBrowserTool` works again on Python 3.14.** The synchronous path — what LangChain and CrewAI call for `.invoke()` — used `asyncio.get_event_loop().run_until_complete()`, and since 3.14 `get_event_loop()` raises `RuntimeError: There is no current event loop` rather than creating one. `auto-browser-langchain` advertises 3.14 in `requires-python` and its classifiers, and nothing tested the package at all, so this shipped unnoticed. Now uses `asyncio.run()`, which is correct on every supported version; callers already inside an event loop should await `_arun` via `ainvoke` (#110).
- **`python -m app.harness.run` no longer crashes when a task fails to converge.** The run record's `candidate` field is present but null unless the run succeeded, and `payload.get("candidate", {})` returns `None` rather than the `{}` default when a key exists with a null value — so the summary line raised `AttributeError` instead of printing, exactly when an operator was reading the output to find out why the run failed. The `--json` path was unaffected (#108).

### Changed
- **The auth-profile containment guards are now pinned by tests.** `BrowserAuthProfileService` handles stored browser auth state (cookies, session tokens) and was the least-covered module in the controller at 22%; its guards were correct but untested, so a refactor that weakened one would have shipped green. Rejection behaviour is now covered for Zip Slip member names, symlink/hardlink/device archive members, containment on a separator boundary rather than a raw string prefix, the profile-name whitelist, and archive-path traversal — plus the end-to-end property that a malicious archive writes nothing outside the root. Each guard's test was verified by breaking the guard and confirming the failure (#105).
- **The MCP stdio bridge's failure paths are covered.** `app/mcp_stdio.py` — what `uvx auto-browser-mcp` runs — went from 58% to 99%, covering HTTP errors being returned rather than raised (so the server's JSON-RPC error reaches the client instead of a transport crash), teardown swallowing a dead connection, batch/non-object rejection, unreachable-endpoint handling, and protocol-version fallback when the response header is absent (#106).
- **The quality gates now cover the whole repo instead of the controller.** `make lint` ran on a hardcoded path list that excluded `client/`, `integrations/`, `benchmarks/` and `examples/` — including two packages published to PyPI — and four real errors had accumulated there unseen; all are fixed and the scope is now the repo. A root `ruff.toml` gives the previously-unlinted areas the controller's settings rather than Ruff's bare defaults. `auto-browser-langchain` went from **zero tests to 94%** with a CI job that runs 3.11 and 3.14, and `auto-browser-client` gained a coverage gate at 85% (it measures 90%) — the controller had enforced 80% while the packages users actually install enforced nothing (#110).
- **Version parity across all nine version strings is machine-enforced.** The release workflow only ever verified the tag against the three published `pyproject.toml` files, which is how v1.4.0 shipped with the runtime version strings still at 1.3.1. `scripts/check_version_parity.py` now checks every location and reports all offenders (#110).
- Roadmap corrected: MCP `resources/subscribe` push notifications were listed under "Next" but shipped in v1.1.0 (#107).

## [1.4.1] — 2026-07-24

### Added
- **Route-presence gate (`controller/tests/test_route_presence.py`).** Router mounting is now verified directly instead of being guarded by a dependency pin. Three independent layers: every `include_router` call site is represented in the OpenAPI surface (13 canaries, each naming its owning module), the surface has not collapsed, and the canaries actually dispatch at runtime. Layer 3 is not redundant — since FastAPI 0.137 `app.routes` holds `_IncludedRouter` wrappers rather than flattened `Route` objects, so introspection and dispatch can diverge (#101).

### Changed
- **`fastapi` cap `<0.137` lifted to `>=0.139.2,<0.140`, and its stated rationale corrected.** The cap claimed 0.137 changed `include_router` internals so every mounted route vanishes silently, and set the condition for lifting it — "revisit behind a route-presence smoke test" — but that test was never written, so the cap could not be evaluated and simply ossified. Measured directly: the app exposes an identical 91-path surface on 0.136.3, 0.137.0 and 0.139.2, with the full suite green on 0.139.2; PR #82, the bump that triggered the cap, was itself fully green. What 0.137 *did* change is `app.routes` holding `_IncludedRouter` wrappers, so nothing may enumerate `app.routes` expecting `.path` (#99, #101, #102).
- **CI lint no longer drifts from the ruff pin it is supposed to enforce.** The lint job hardcoded `ruff==0.15.12` while `controller/requirements-dev.txt` pinned `0.15.21`; Dependabot only bumps the requirements file, so the gate sat 9 patch versions behind and drifted further with every bump — lint could pass in CI and fail locally. The job now reads the pin from the requirements file. Same one-dependency-two-sources failure class as the Playwright drift guarded by `scripts/check_playwright_pins.py` (#101).
- **Richer MCP tool schemas for the lowest-rated tools.** `browser.get_session`, `browser.get_auth_profile`, `browser.activate_tab`, `browser.execute_action`, and `harness.list_runs` (the five tools Glama graded C) now document what they return, where their IDs come from, and every input parameter; shared input fields (`session_id`, tab `index`, `run_id`) gained schema descriptions that flow through to every tool built on them.

### Fixed
- **Compose deploy now wires `APP_ENV` and the v1.4.0 OpenAI-compatible providers into the controller.** `APP_ENV` was absent from `docker-compose.yml`, so `APP_ENV=production` in `.env` never reached the container — it always ran in `development`, silently skipping production hardening. And the new providers (`openrouter`, `xai`, `deepseek`, `minimax`, `openai_compatible`) weren't passed through, so they couldn't be configured through a standard compose deploy despite being supported in code. Both are now in the controller `environment:` block. Caught by smoke-testing the deploy end to end.

## [1.4.0] — 2026-07-09

### Added
- **Any OpenAI-compatible model can now drive the browser.** A single generic adapter serves every model reachable over an OpenAI `/chat/completions` endpoint, so Auto Browser is no longer limited to OpenAI / Claude / Gemini. New providers: `openrouter` (one key → ~every frontier model — Claude, GPT, Gemini, Grok, DeepSeek, Llama, Mistral, Qwen, …), `xai` (Grok), `deepseek`, `minimax`, and `openai_compatible` (a custom base URL for self-hosted Ollama / vLLM / LM Studio, Azure, Together, Groq, Fireworks, …). Vision + function-calling with a content-parse fallback for endpoints that ignore `tool_choice`; DeepSeek runs text-only from the DOM/accessibility outline. Configure via the `*_API_KEY` / `*_BASE_URL` / `*_MODEL` settings in `.env.example` (#85). Supersedes the bespoke MiniMax adapter from #77 — MiniMax is now a generic profile.
- **`browser://audit/events` MCP resource** — list and read recent audit events across sessions over MCP, guarded on `manager.list_audit_events` so it no-ops where unavailable (#86, adopted from #48 by @luohui1).
- **CI guard for Playwright pin parity** between the controller (`requirements.txt`) and browser-node (`package.json`) so a single-side bump can no longer merge; and `fastapi` capped `<0.137`, whose router-internals change silently drops every mounted route (#84).

### Fixed
- **Controller could not connect to the browser node in compose deployments.** Dependabot #60 bumped browser-node's npm `playwright` to 1.61.1 while the controller's pip pin stayed at 1.60.0; Playwright's websocket protocol requires an exact client/server version match, so every `docker compose up --build` crash-looped readiness. Fixed in two steps: first re-aligned both sides to the known-good 1.60.0, then landed the coordinated upgrade pinning `playwright==1.61.0` on both pip and npm (#76 by @itsreese83, who also diagnosed the mismatch).
- `scripts/doctor.sh` now dumps controller and browser-node container logs when the readiness probe times out, so compose-smoke failures are diagnosable from CI output.

### Changed
- Dependency bumps: `uvicorn` 0.49.0 → 0.50.1 (#79), `pydantic-settings` 2.10.1 → 2.14.2 (#80), `pillow` 12.2.0 → 12.3.0 (#81), and dev `ruff` 0.15.12 → 0.15.20 (#78).

## [1.3.1] — 2026-07-01

### Changed
- **`browser_manager.py` is now a pure facade + composition root** (1,284 → 769 lines). The domain logic that remained after the v1.2 service extraction moved into `app/browser/services/` (fork / shadow-browse / network log → sessions & diagnostics; settle / action verification → actions; platform detection + auth-state info → auth profiles), and ~50 delegation shims with zero callers were deleted. The public API and the private seams that tests patch are unchanged.
- **Fork state exports are encrypted at rest.** `fork_session` now routes its storage-state export through `AuthStateManager`, so exported cookies/localStorage are Fernet-encrypted whenever `AUTH_STATE_ENCRYPTION_KEY` is set (previously always plaintext JSON, regardless of settings).
- **Shadow-browse state never touches disk.** `enable_shadow_browse` hands the exported storage state to the headed context as an in-memory dict instead of writing a plaintext temp file.

### Fixed
- **Download capture tasks can no longer be garbage-collected mid-flight.** Page `download` handlers now go through `spawn_background_task`; the event loop keeps only weak task references, so the old bare `asyncio.create_task` could silently drop a download and its audit record.
- **Shadow-browse failures roll back cleanly.** If navigation in the headed clone fails, the headed Chromium process is closed and the half-registered session is removed (previously both leaked until manual cleanup).
- **Page listeners survive object-id reuse.** Sessions track listener-attached pages in a `WeakSet` instead of raw `id()` values, so a recycled id can no longer cause a new page to skip listener attachment.
- `shutdown()` is idempotent (the Playwright handle is cleared after stop), and `_assert_url_allowed` matches host patterns case-insensitively on all platforms.

## [1.3.0] — 2026-07-01

### Added
- **Operator dashboard: run replay view.** Open a completed agent run by job id to see its action order, approvals, final status, and screenshot artifacts, reusing the existing `/agent/jobs` and `/approvals` endpoints. All untrusted run data renders via text nodes and safe cell helpers (never `innerHTML`).
- **Operator dashboard: auth profile setup wizard.** A four-step flow — name a profile and start a login session, complete login by hand in the takeover window, save the captured auth state as a named profile, and reopen a session from any saved profile.
- **Local fixture server + optional live execution** (`scripts/fixture_server.py`, `scripts/fixture_live.py`): serve `evals/fixtures/` over loopback and drive the real controller (create session → navigate → observe) against a fixture. Opt-in; requires Playwright browsers and never runs in default CI.
- **WebArena Stage 0 executable contracts** (`benchmarks/webarena/`): typed `TaskContract`s parsed from the manifest, an environment-revision pin (null until a reviewed SHA is set), and a runner with `validate`/`execute` modes that materializes the trace/actions/screenshots/model_decisions evidence layout. Lane stays tracked-only until pinned.
- **Verifier lane adapter** (`benchmarks/adapters/verifier_adapter.py`): maps an `AgentRunResult` into the CUAVerifier and Online-Mind2Web evidence lanes. Never scores — `verifier_result` is always `None` and records are `scored: false`.
- **Closed-tab recovery fixture + regression** (`closed-tab-recovery`): the fixture-eval mandatory set and a controller test now cover closing the active tab and recovering to a usable, foregrounded tab.
- **MCP resources & subscription examples** (`examples/mcp-resources.md`) with a doc-sync test that keeps the documented URIs, methods, and error codes aligned with `mcp_transport.py`.
- **Live-free coverage** for the provider base layer (readiness checks, decision-parse ladder, error extraction), the stealth timing/fingerprint layer, and the mesh nonce replay cache. Controller coverage ratcheted upward while keeping the 80% gate green and avoiding live browser/network dependencies.
- **Scheduled dependency audit** is provided by the existing `dependency-audit` CI job (pip-audit) plus GitHub Dependabot alerts.

### Changed
- **Startup warns when `API_BEARER_TOKEN` is unset in non-production.** Production already hard-fails; non-production now surfaces a runtime-policy warning so a reachable dev/staging instance is not silently served unauthenticated.
- **Provider decision-parse ladder** narrows its fall-through handlers from bare `except Exception` to `(ValidationError, ValueError)`, so a genuine bug propagates instead of being masked as a parse miss.

### Fixed
- **Codespaces stack no longer double-binds ports.** `docker-compose.codespaces.yml` now tags its `ports` lists with `!override` so they replace the base `127.0.0.1` bindings instead of appending `0.0.0.0` on top (which failed with "address already in use" and broke the devcontainer `postStartCommand`).
- **`GET /sessions` no longer 500s after the browser is closed from VNC** (external fix, thanks @gmother): session summary detects a disconnected page and marks the session `interrupted`/`live:false` instead of raising.

### Security
- Resolved all open Dependabot alerts by bumping pinned dependencies: `starlette` 1.0.1 → 1.3.1, `cryptography` 46.0.7 → 49.0.0, plus `redis` 8.0.1, `pyotp` 2.10.0, `prometheus-client` 0.25.0, and Playwright/GitHub Actions updates.

## [1.2.1] — 2026-06-10

### Added
- Published to PyPI: `auto-browser-client` (SDK + bridge), `auto-browser-langchain` (LangChain/LangGraph/CrewAI adapters), and `auto-browser-mcp` (metapackage so `uvx auto-browser-mcp` runs the stdio bridge directly).
- Added the `auto-browser-mcp` console script: the stdio bridge now lives in the client package as `auto_browser_client.mcp_bridge` and ships on PyPI. The controller's `app.mcp_stdio` copy remains for the Docker/Glama entrypoint; a guard test keeps the two copies byte-identical and their header constants in sync with `app.mcp_transport`.
- Added a tag-triggered `release.yml` workflow: verifies tag/version agreement, builds the three distributions, publishes via PyPI trusted publishing (OIDC — no stored tokens), and creates the GitHub release with CHANGELOG notes and artifacts attached.
- Added `scripts/extract_changelog.py` to pull one version's section out of CHANGELOG.md for release notes, failing loudly when the section is missing.

### Changed
- Claude Desktop / Cursor / MCP client examples now use `uvx auto-browser-mcp` as the primary setup path, with the repo-checkout script as fallback.
- Filled out PyPI packaging metadata (license, readme, classifiers, keywords, project URLs) for the client and LangChain packages.
- Bumped controller, client, LangChain integration, and browser-node package metadata to `1.2.1`, and refreshed release-facing version strings.

## [1.2.0] — 2026-06-10

### Added
- Added Witness receipt chain verification: `WitnessRecorder.verify` walks a session's full hash chain and reports the first divergent receipt, exposed as `GET /sessions/{session_id}/witness/verify` and the read-only `browser.verify_witness` MCP tool (curated profile). Detects altered, reordered, truncated, and unparseable receipts.
- Added orphaned-container reaping: `docker_ephemeral` startup now removes session containers labeled `auto-browser.managed=true` that a crashed or killed controller left behind, honoring `ISOLATED_BROWSER_KEEP_CONTAINERS`.
- Added resource caps for per-session browser containers: `ISOLATED_BROWSER_MEM_LIMIT` (default `4g`), `ISOLATED_BROWSER_PIDS_LIMIT` (default `2048`), and `ISOLATED_BROWSER_CPUS` (default off).
- Added a Python 3.14 lane to the host-tests CI matrix alongside 3.11.
- Added a private security-advisory reporting channel to `SECURITY.md` (GitHub private vulnerability reporting is enabled on the repo).

### Changed
- Unified the stealth user-agent pools: `stealth.fingerprint.CHROME_UA_POOL` (refreshed to Chrome 149, June 2026 stable) is now the single source for both the fingerprint layer and the `USER_AGENT_POOL` config default, which previously shipped a stale Chrome 122–124 list. The fingerprint pool no longer cycles Firefox/Safari UAs on the Chromium engine — an engine/UA mismatch is itself a bot signal.
- Upgraded pinned dependencies: Playwright 1.56.0 → 1.60.0 (controller and browser-node together, keeping the WS protocol versions aligned), uvicorn 0.35.0 → 0.49.0, APScheduler 3.11.0 → 3.11.2, and raised dev floors for pytest-asyncio (>=1.4.0) and pytest-cov (>=7.1.0). Closes the open Dependabot batch.
- Refreshed default model IDs: `CLAUDE_MODEL` → `claude-sonnet-4-6`, `OPENAI_MODEL` → `gpt-5-mini`, `GEMINI_MODEL` → `gemini-3.5-flash` (Gemini 2.0-era defaults stopped being served on 2026-06-01).
- Refreshed `ROADMAP.md` to the v1.2.0 surface (the "Now" section had been frozen at v1.0.5) and corrected the tool count.
- Documented the dashboard `#token=` URL-hash trade-off in the production hardening guide.
- Bumped controller, client, LangChain integration, and browser-node package metadata to `1.2.0`, and refreshed release-facing version strings in the dashboard badge, webhook user-agent, README highlights, launch notes, and good-first-issue docs.

### Fixed
- Fixed silent `except: pass` blocks in cleanup and capture paths (session isolation, network inspector, navigation settle, bot-challenge probe, workflow run listing): failures are now logged with context instead of vanishing. A takeover request that fails after a detected bot challenge now logs a warning. Deliberate recovery cascades (provider decision parsing, event queue removal) were reviewed and left as-is.

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
