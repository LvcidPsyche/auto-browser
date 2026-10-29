# Auto Browser Deployment Guide

This is the recommended deployment shape for a **single-tenant, trusted-operator** production install.

## Recommended topology

- Auto Browser controller + browser node on one private host
- Front the controller and takeover UI with **Cloudflare Access** or **Tailscale**
- Keep published ports bound to `127.0.0.1`
- Use `docker_ephemeral` session isolation if the operator may touch multiple accounts/workflows (what each mode separates, and what it does not: [session-isolation-audit.md](./session-isolation-audit.md))

## Required production settings

At minimum:

```env
APP_ENV=production
API_BIND_SCOPE=exposed
API_BEARER_TOKEN=<strong-random-secret>
REQUIRE_OPERATOR_ID=true
AUTH_STATE_ENCRYPTION_KEY=<44-char-fernet-key>
REQUIRE_AUTH_STATE_ENCRYPTION=true
REQUEST_RATE_LIMIT_ENABLED=true
METRICS_ENABLED=true
CONTROLLER_ALLOWED_HOSTS=<controller-ingress-hosts>
```

Strongly recommended:

```env
SESSION_ISOLATION_MODE=docker_ephemeral
MAX_SESSIONS=1
ALLOWED_HOSTS=<your-real-allowlist>
STATE_DB_PATH=/data/db/operator.db
ARTIFACT_RETENTION_HOURS=168
UPLOAD_RETENTION_HOURS=168
AUTH_RETENTION_HOURS=168
```

`CONTROLLER_ALLOWED_HOSTS` protects the controller itself from unexpected HTTP Host headers.
Keep `ALLOWED_HOSTS` for browser navigation targets, and set `CONTROLLER_ALLOWED_HOSTS`
to the hostnames operators use to reach the controller, such as `browser.example.com`
or `localhost,127.0.0.1,::1` for a local-only install.

When it is unset and the controller runs without a bearer credential (a loopback
deployment), only loopback Host names — `localhost`, `127.0.0.1`, `[::1]` — are
answered. An unauthenticated API that accepts any Host header can be driven by a web
page through DNS rebinding, so this default holds until you either list the names
clients use or configure `API_BEARER_TOKEN`. `/healthz` is exempt.

## Auth profiles

Auto Browser now supports reusable named auth profiles under:

```text
/data/auth/profiles/<profile-name>/
```

Use them when you want a human to log in once and let future sessions resume from saved browser state without passing raw storage-state file paths around.

Recommended pattern:
- use one profile per account/workflow
- keep auth-state encryption enabled in production
- treat profile names as operator-facing labels, not secrets

## Provider authentication choices

You now have two viable ways to authenticate model providers:

### Option A — API keys

Use:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`

This is still the cleanest option for CI and broadly automated installs.

### Option B — subscription-backed CLIs

Use this on a trusted private box if you already run:

- `codex` via ChatGPT/Codex login
- `claude` via Claude Code login/subscription
- `gemini` via Gemini CLI login

Set:

```env
OPENAI_AUTH_MODE=cli
CLAUDE_AUTH_MODE=cli
GEMINI_AUTH_MODE=cli
CLI_HOME=/data/cli-home
INSTALL_AGENT_CLIS=true
```

`INSTALL_AGENT_CLIS=true` builds the three CLIs (and Node.js, about 750 MB) into
the controller image, which leaves them out by default. Rebuild after setting it:
`docker compose build controller`. When a `cli` provider's binary is missing,
production startup fails with an error naming this setting, and
`GET /agent/providers` shows the same hint.

Then copy the signed-in CLI state into the mounted data directory:

```bash
mkdir -p data/cli-home
rsync -a ~/.codex data/cli-home/.codex
cp ~/.claude.json data/cli-home/.claude.json
rsync -a ~/.claude data/cli-home/.claude
rsync -a ~/.gemini data/cli-home/.gemini
```

Treat `data/cli-home` like a password vault. Never commit it.

If the easiest path is to sign in on the target box directly, use:

```bash
./scripts/bootstrap_cli_auth.sh codex
./scripts/bootstrap_cli_auth.sh claude
./scripts/bootstrap_cli_auth.sh gemini
```

That helper is for the default writable `/data/...` auth cache flow, and it needs `INSTALL_AGENT_CLIS=true` in `.env` (it says so and stops otherwise). It opens the provider CLI inside the controller image with `HOME=$CLI_HOME` (normally `/data/cli-home`), so the resulting login state lands in the mounted `./data` directory where `*_AUTH_MODE=cli` expects it.

If the target machine already has those subscription logins locally, prefer the host-mount override instead of copying caches:

```bash
CLI_HOST_HOME=/home/botuser \
OPENAI_AUTH_MODE=cli \
CLAUDE_AUTH_MODE=cli \
GEMINI_AUTH_MODE=cli \
docker compose -f docker-compose.yml -f docker-compose.host-subscriptions.yml up -d --build
```

That override builds the controller with the provider CLIs, mounts `~/.codex`, `~/.claude`, `~/.claude.json`, and `~/.gemini` read-only at the same home-path inside the container, and sets `CLI_HOME` to that host-style home. If your login home is different, change `CLI_HOST_HOME`. Do not use `bootstrap_cli_auth.sh` in that mode; sign in on the host first and then start the override.

If Codex subscription auth still fails inside Docker, switch only OpenAI to the host bridge:

```bash
mkdir -p data/host-bridge
python3 scripts/codex_host_bridge.py --socket-path data/host-bridge/codex.sock
```

To keep that bridge running like a host-local skill, install the included user-service template:

```bash
mkdir -p ~/.config/systemd/user
cp ops/systemd/codex-host-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-host-bridge.service
```

Then:

```env
OPENAI_AUTH_MODE=host_bridge
OPENAI_HOST_BRIDGE_SOCKET=/data/host-bridge/codex.sock
```

That keeps `codex` on the host, reuses the host login state directly, and lets the container call it over a shared Unix socket, so the controller image does not need the CLIs for OpenAI.
The controller now health-checks that socket and the bridge kills stuck host `codex` jobs after 55 seconds by default.
Treat the socket as a host-trust boundary: any local process that can connect to it can trigger host-side `codex exec`.

In `APP_ENV=production`, startup now fails fast if:
- any `*_AUTH_MODE` value is not one of its supported modes (`api`, `cli`, and for OpenAI also `host_bridge`)
- a provider is set to `cli` mode but its CLI binary is missing
- OpenAI is set to `host_bridge` mode but the bridge socket is missing, stale, not a real Unix socket, or failing `/healthz`
- `CLI_HOME` is set but the expected auth-state files are missing for that CLI

## Generate an auth-state encryption key

```bash
python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
```

## Local/private production run

```bash
cp .env.example .env
# edit .env with the required production values above

docker compose -f docker-compose.yml -f docker-compose.isolation.yml up -d --build
```

## Health and readiness checks

```bash
curl -fsS http://127.0.0.1:8000/healthz | jq
curl -fsS http://127.0.0.1:8000/readyz | jq

# The rest need the bearer token, and with REQUIRE_OPERATOR_ID=true an operator id
# (/metrics takes the token only). Export API_BEARER_TOKEN from .env first.
AUTH=(-H "Authorization: Bearer $API_BEARER_TOKEN" -H "X-Operator-Id: ops")
curl -fsS "${AUTH[@]}" http://127.0.0.1:8000/readiness | jq
curl -fsS "${AUTH[@]}" http://127.0.0.1:8000/metrics | head
curl -fsS "${AUTH[@]}" http://127.0.0.1:8000/maintenance/status | jq
```

`/readiness` answers `503` when a check fails, so `-f` prints nothing; drop `-f` to see which check.
If `METRICS_ENABLED=false`, skip the `/metrics` check; the endpoint returns `404`.

## Witness receipts: what a signature proves

Each receipt is hash-chained to the one before it and signed with an Ed25519 key. The key is generated on first use in `<WITNESS_ROOT>/keys`, beside the receipts it signs. That decides what the signature can prove:

- **It proves the receipts are consistent and were signed by that key.** `GET /sessions/{id}/witness/verify` and the `browser.verify_witness` tool report this: the hash walk, the anchor, and a `signatures` block. A chain with no signed receipt fails the `signatures` check, and so does an unsigned receipt after a signed one. The top-level `valid` passes only when all three do. `chain_valid` is the hash walk and the anchor alone, and `reason` says which check failed. Missing evidence is never `valid`: a chain with no receipts (deleted, or never written), a chain whose anchor file is missing or unreadable (`anchor.status` is `anchor_missing` or `anchor_unreadable`), and a chain shorter than its anchor all fail. An id with no session record, no chain and no anchor answers `404`, for verify and for the bundle. Chains written before 1.7.0, when anchors arrived, are anchored as they are on the first start after upgrading (the root is then marked `.anchors-migrated` and no later start anchors anything), so they keep verifying; anything lost from them earlier cannot be detected. A later receipt never moves the anchor over a chain that was cut short or lost its anchor: the chain stays flagged, and the receipt is still recorded. With `WITNESS_ENABLED=false` no receipts are recorded, so a session that has none verifies `false`; receipts recorded earlier still verify. Anyone who can write the witness directory can still make a cut chain verify, by rewriting the anchor, by deleting the chain together with its anchor, or by deleting `.anchors-migrated` before a restart: for evidence that must survive that, pin a head hash off the box (below). A crash or a full disk between a scope's first receipt and its anchor leaves that chain `anchor_missing` for good, and a receipt whose anchor update failed is unprotected until the next successful append moves the anchor. To recover a chain you have checked by other means, delete `<WITNESS_ROOT>/.anchors-migrated` and restart: that anchors every chain without an anchor as it stands, so it also anchors any cut chain whose anchor was deleted.
- **It does not prove anything against someone who controls the box.** Whoever can write the witness directory can also read the key, so they can edit receipts and sign the result. A bundle that verifies tells a recipient the receipts match the key in the bundle, not that the key was never in the wrong hands.

For evidence that survives a compromised controller host, keep a head hash from before the compromise and check it when you verify: `python scripts/verify_witness_bundle.py bundle.json --expect-head <head_hash> --expect-key-id <key_id>`. The `head_hash` and `signing_key_id` come from `GET /sessions/{id}/witness/bundle` or from the verify result. Sending a copy of each receipt to a hosted Witness service (`WITNESS_REMOTE_URL`), or copying the key id and head somewhere the controller host cannot write, works as well. Without such a pin, a recipient can only check that the bundle is internally consistent.

## What PII scrubbing covers, and what it does not

`PII_SCRUB_*` matches known secret and personal-data shapes (emails, phone numbers, card numbers, API tokens, private-key blocks and so on) in a few specific outputs. It is a safety net for those outputs, not a filter over everything the browser can see.

**Scrubbed**

- **Screenshots** (`observe`, `capture_screenshot` and the before/after action snapshots): matching words are painted over using OCR boxes. This needs the `tesseract` binary, which only the Docker image ships. Outside Docker, or with `OCR_ENABLED=false`, screenshots are returned **unscrubbed**: `/readiness` reports a `screenshot_scrubbing` warning, the controller logs one warning, and each result carries `screenshot_pii_scrub: {"applied": false, "reason": ...}`. A redaction that fails while it runs (an image Pillow cannot process) is reported the same way, with reason `redaction_failed`, and audited as `failed`, not `ok`. OCR is also best-effort, so small or low-contrast text can be missed.
  Set `PII_SCRUB_SCREENSHOT_STRICT=true` to withhold instead: when a screenshot cannot be scrubbed (OCR unavailable, or a redaction that fails) it is deleted and the call fails with code `screenshot_withheld` (HTTP 503), and with OCR unavailable `/readiness` reports `screenshot_scrubbing` as `fail` rather than `warn`. `PII_SCRUB_ACTION_SCREENSHOTS=false` still leaves the before/after action snapshots unredacted, by choice. An action whose after-screenshot is withheld has still run: it is reported and recorded, with `observation_error` in place of the image. The default is `false`.
- **Network inspector bodies and URLs**: request and response bodies stored by the network inspector. Text bodies are scrubbed whatever their content type says; binary content types and bytes that are not UTF-8 are stored as they are. URL-encoded form bodies are matched as they arrived and with their `%XX` escapes and `+` signs decoded, and only the stretch that matches is replaced, so the rest of the form is stored byte for byte. A parameter whose name marks a secret has its whole value redacted whatever it looks like, and keeps its name. Any segment of the name can mark it: a word of `password`, `passwd`, `passphrase`, `pwd`, `pass`, `secret` or `token` (`password_confirmation`, `user[password][new]`, `access_token`, `csrf_token`, `db_pass`, `wifiPass`, and also `token_type`), a trailing `key` (`api_key`, `apiKey`), or exactly `cvv`, `cvc`, `csc` or `pin`. A name that merely holds the letters (`tokens`, `tokenizer_name`, `secretary`, `spin`, `keyboard`, `passport`, `passenger_count`, `passed`) does not count. Neither do `public_key`, `publishable_key` and `site_key`, which are shown to every visitor, nor a paging cursor, which points at another page of a list: `page_token` with `next`, `prev` or `previous` in front of it (`next_page_token`, `nextPageToken`, `prev_page_token`, `previousPageToken`). `page_token`, `pageToken` and `page_token_value` are secret names, because a Facebook page access token is the value of one. (The name rule is all that spares a cursor: in a form or a query, 30 or more base64 characters right after `token=` are still redacted by the base64-secret pattern, name included, so a long `next_page_token` there is stored as `next_page_[REDACTED]`.) JSON and JavaScript bodies are matched as they arrived, with their `\uXXXX` escapes and `\/` written out (all but the quote, backslash, control-character and surrogate escapes, which must stay escaped to keep the JSON valid), and with only the escapes of letters, digits and `@._%+-` written out; every match is redacted in the text as it arrived. A JSON member whose name marks a secret by the same rule (`access_token`, `refresh_token`, `id_token`, `client_secret`, `api_key`) has the text of its string value redacted and keeps its name, so an OAuth token response stays valid JSON. A body with no escape is read the same way as one with escapes, and in JSON the password, `api_key`, Azure-secret, base64-secret and GCP service-account key-id patterns redact the value alone, so the quotes around it stay and the body still parses when the value is a string (a service-account key file keeps its `"private_key_id"` member). A card number is found beside other groups of digits too (`4111 1111 1111 1111 12/27`). The query string and the fragment of request URLs and of the `Referer` and `Location` headers are scrubbed the same way (an OAuth implicit grant or an OIDC response in fragment mode carries `access_token` and `id_token` in the fragment), and a query or a fragment longer than `NETWORK_INSPECTOR_BODY_MAX_BYTES` (counted in UTF-8 bytes) is replaced, each on its own; a query that is too long takes the fragment with it. A redaction in any of them sets the entry's `pii_redacted`. The URL of a failed request that is kept in the session's `request_failures` (returned by `get_request_failures`, and the last ten in every observation) goes through the same scrub, with the same size cap and under the same `PII_SCRUB_NETWORK` setting, whatever `NETWORK_INSPECTOR_ENABLED` and `NETWORK_INSPECTOR_CAPTURE_BODIES` say; with network scrubbing off it is stored as it is, except that a query or a fragment over the cap is still replaced, as in the inspector's URLs (under `PII_SCRUB_ENABLED=false` the inspector stores it whole). A body the scrubber cannot process, or whose result is not text, is withheld, not stored. When a scrub fails, the warning in the log names the URL as the inspector stores it (its query scrubbed) and the exception type for a body, and only the origin and the exception type for a URL; never the body. A PEM private-key header with no `END` line redacts everything to the end of the text, so in a form it takes the parameters after it too. Network hooks (`POST /sessions/{id}/network/hooks`) match the stored, redacted URL: a hook registered on a query value (`*email=ops%40example.com`) no longer fires, while one on the path or on a parameter name still does. Headers such as `Authorization` and `Cookie` are masked separately.
- **Console messages** returned by `get_console_messages`.

**Not scrubbed**

- **Page content, by design**: `get_html`, `find_elements`, the DOM outline and element text in an observation, cookies and storage, downloads read with `read_download`, and the output of `eval_js`.
- **Raw CDP**: `raw_cdp_command` returns what the browser returns. `DOM.getDocument` with `{"depth": -1, "pierce": true}` includes a password typed into a page, read from the input's user-agent shadow text. The accessibility tree masks password values (`•••`), but the raw DOM does not. Treat anything an agent types into a page as readable by every caller allowed to use raw CDP.
- **The last console messages inside an observation** (`console_messages` in an `observe` result), and page errors. Use `get_console_messages` for the scrubbed view. (The URLs in request failures are scrubbed: see the network inspector entry above.)
- **The page's own URL** (`url` in every observation, and in the prompts of the built-in agent loop), which keeps, for example, an OAuth implicit grant's `#access_token=...`.
- **Parts of network traffic the inspector stores as they are**: double-encoded values (`email=ops%2540example.com`, or a login redirect carried inside a parameter: `redirect=https%3A%2F%2Fx.test%2Fcb%3Femail%3Dops%2540example.com`); JSON carried as a string inside JSON or inside a form value (`{"body": "{\"password\": ...}"}` as written in the body, `payload=%7B%22access_token...`, GraphQL variables sent as a string); JSON or form data sent as `text/plain` or with no content type (the `navigator.sendBeacon` default, and a `fetch` with a string body), which is scrubbed as plain text: no escapes are written out and no member is redacted by its name; `multipart/form-data` bodies, which are matched as plain text, so no part is redacted by its field name (a `FormData` password is stored as it is) and a part that carries a transfer encoding is not scrubbed at all; the URL path and userinfo (`https://user:secret@host/`); headers outside the mask list (`Link`, `Content-Location` and `Refresh` can carry URLs); and request bodies with a binary content type. In text and JSON bodies the `api_key_param` pattern also redacts the `&` that follows the value and the next parameter's name with it (`"next": "https://x.test/p?client_secret=Opaque-Secret-99&page=2"` is stored as `client_secret=[REDACTED]=2`); in a form or a query it stops at the `&`.
- **Playwright traces**, which are off by default since 1.10.2. With `ENABLE_TRACING=true` the session's `trace.zip` records every request URL, every header (`Authorization` and `Cookie` included), the request and response bodies as they were sent, and the text typed into the page, passwords included, without any of the scrubbing above. It is written when the session closes and served under `/artifacts/{session_id}/trace.zip` to whoever may use the session (`GET /sessions/{id}/trace` returns the path). Before 1.10.2 tracing was on unless `ENABLE_TRACING=false` was set, and turning it off does not remove or scrub a trace already written: delete old `trace.zip` files under `ARTIFACT_ROOT` that may hold credentials.
- **Known gaps in the network inspector's scrub**: the OAuth `code` parameter is recognised only under its plain name (`co%64e=` is stored as it is), and a `code` that is the first parameter of a form body, as opposed to a URL query, is not recognised either. In JSON the password, `api_key`, Azure-secret, base64-secret and GCP service-account key-id patterns redact the value alone, so the stored body still parses (`{"password":"[REDACTED]"}`). Two patterns still run on, to the end of the line and to the end of the text: a `Cookie: ...` line inside a JSON string, and a private-key block with no `END` line, take the rest of the body with them and leave JSON that no longer parses. A value that is not a string under a password name (below) is the third case. Outside JSON (`text/plain`, untyped, HTML, XML) the password pattern still takes its name and quotes with the value (`{[REDACTED],"n":1}`). A value of four or more characters under a password name that is not a string (`"password": 12345678`) is matched by the password pattern too, which runs on through the comma or the closing brace. A secret-named JSON member is redacted only when its value is a string: a number (`"pin": 1234`), an array or an object is stored as it is, and so are the members inside an object under a secret name (`"password": {"new": "x"}`) unless their own names mark a secret. A card number is found among other digit groups only along group boundaries and in a run of at most seven groups, and a group of four or more digits directly before it takes its room: `12345 4111 1111 1111 1111`, and a card glued to other digits with no separator (`411111111111111112`), are stored as they are. The same goes for a card that follows a JSON escape that stays escaped and is written with digits only (`\u0009 4111 1111 1111 1111`, a tab or a quote), because the escape's digits join the card's. The secret-name rule does not know every secret name. These are stored as they are: `passcode`, `otp`, `mfa_code`, `totp`, `verification_code`, `cvv2`, `card_cvv`, `security_code`, `ssn`, `auth`, `authorization`, `credentials`, `session_id`, glued lowercase names (`accesstoken`, `refreshtoken`, `sessiontoken`, `clientsecret`, `secretkey`, `privatekey`), `csrfmiddlewaretoken` and names in other languages. Some ordinary names are redacted too: `primary_key`, `sort_key`, `cache_key`, `idempotency_key`, `license_key`, `token_type`, `token_count`, `continuation_token`, `password_policy`, `password_hint`, `passwordless`, `secret_question`, `boarding_pass`, `pass_through`, `pass_type` and `pass_id`, and `bypass`, `compass` and other names that end in `pass`, `pwd` or `passwd`, whose value the password pattern redacts when it has four or more characters and no space. The rule runs only while `password_field` is among the enabled patterns (`PII_SCRUB_PATTERNS`), and its hits are reported under that name. In a form or a query that has a `%XX` escape or a `+` anywhere in it, including one written by an earlier redaction, a value `Bearer` followed by another parameter takes the `&` and the next name with it (`token_type=Bearer&expires_in=3599` is stored as `token_type=%5BREDACTED%5D3599`): over-redaction, not a leak. An OAuth implicit response triggers it without any escape of its own, because its `access_token` is redacted first and that writes `%5B` (`?access_token=abcdefgh123&token_type=Bearer&expires_in=3599` is stored as `?access_token=%5BREDACTED%5D&token_type=%5BREDACTED%5D3599`). **Cost**: the scrub runs on the event loop and is bounded by the caps (the query, the fragment and the body each up to `NETWORK_INSPECTOR_BODY_MAX_BYTES`). A hostile page can make one request event cost about 150 ms and one response event about 125 ms (23 ms before the scrub work): a 16 KB JSON body with escapes costs about 72 ms, a 16 KB query about 25 ms, and a URL header with a 16 KB query and fragment about 49 ms. A page can set a `Location` request header itself; Chrome cuts a `Referer` over 4 KB to the origin. A failed request adds one URL scrub, about 25 ms at the cap. Against the code before the scrub work, a 16 KB JSON body without escapes costs about 1.1x to 2x, one with escapes up to about 3.4x, and a form of `api_key` pairs about 3.5x.

If a workflow types real credentials into a page, scrubbing does not protect them from the tools above: restrict who can call them, and use the approval gates and `ALLOWED_HOSTS`.

## Common pitfalls and failure modes

### Browser session dies or Chromium crashes

- recreate the session first; do not assume browser state is still valid
- check controller logs and browser-node logs before retrying the same action loop
- inspect the latest artifacts/screenshots to confirm where the flow broke

### Saved auth profile stops working

- the site may have expired cookies, revoked the session, or triggered MFA again
- recreate the profile with a fresh manual login
- avoid reusing one profile across multiple unrelated accounts or workflows

### noVNC works locally but not remotely

- keep controller and noVNC ports bound to `127.0.0.1`
- verify your access layer first: Cloudflare Access, Tailscale, or reverse tunnel
- if remote takeover breaks, treat it as a gateway/tunnel problem before debugging Playwright

### Production boot fails closed

- this is intentional in `APP_ENV=production`
- check required env vars first: bearer token, operator ID enforcement, encryption key, rate limiting
- if using `*_AUTH_MODE=cli` or `host_bridge`, verify the expected auth files or socket actually exist

### Actions fail after redirects or external handoffs

- update `ALLOWED_HOSTS` to include the real target domains involved in login, redirects, uploads, and downloads
- remember that identity providers, CDNs, and file hosts may differ from the main app domain

### OCR output looks incomplete or wrong

- Tesseract OCR is helpful, not authoritative
- use screenshots and DOM observations as the primary source of truth
- expect worse OCR quality on small text, low-contrast UIs, and dense dashboards

## Gateway recommendations

Use **one** of:

- **Cloudflare Access** in front of the controller + noVNC paths
- **Tailscale** and keep the whole stack private
- the included reverse-SSH path for bastion-style access when direct reachability is not available

Do **not** expose raw controller or noVNC ports directly to the public internet.

noVNC (6080) is the only way to the desktop. x11vnc listens on no TCP port: it serves
RFB only on a unix socket inside the container, in a directory only the `browser` user
can enter, and websockify proxies noVNC to that socket. A native VNC viewer cannot
connect to the container; use the noVNC page.

`VNC_PASSWORD` adds VNC authentication to noVNC, on the shared browser-node and on
isolated session containers; noVNC prompts for it. Set it whenever 6080 is reachable
beyond loopback — but treat it as a second layer, not the gateway: the VNC protocol
uses only the first 8 characters and a DES-based challenge.

Chromium runs in the same container as noVNC, so every page it loads can reach
`ws://127.0.0.1:6080`. Two things keep such a page out:

- **The WebSocket origin guard** accepts a WebSocket only when its `Origin` is the
  host the client connected to and that host is a loopback name or listed in
  `NOVNC_ALLOWED_HOSTS` (so DNS rebinding cannot pass as the noVNC page). This stops a
  visited page from opening its own WebSocket to noVNC. It cannot tell the noVNC page
  from a copy of it: the page's own WebSocket carries noVNC's own origin.
- **The no-framing headers** (`X-Frame-Options: DENY` and
  `Content-Security-Policy: frame-ancestors 'none'` on every noVNC HTTP response)
  stop a visited page from embedding `http://127.0.0.1:6080/vnc.html` in a frame and
  so putting a live desktop behind its own page. A page can still link to noVNC or open
  it in a window, but then it is a separate cross-origin document the page cannot read
  or script.

If takeover fails with "Invalid Origin Header" in `/tmp/novnc.log`:

- the takeover URL uses a non-loopback host name (a LAN IP, a bastion, a domain):
  add it to `NOVNC_ALLOWED_HOSTS`, e.g. `vnc.example.com`;
- a proxy in front of port 6080 rewrites the `Host` header: add the takeover
  page's origin to `NOVNC_ALLOWED_ORIGINS`, e.g. `https://vnc.example.com`.

Inside the browser container, Chromium, Xvfb, x11vnc and noVNC run as the
unprivileged `browser` user (uid 10001). The entrypoint starts as root only to take
ownership of the mounted `/data/profile` and `/data/downloads`, so on the host
`./data/browser-profile` and `./data/downloads` end up owned by uid 10001.

## Backups

Back up at least:

- `/data/db/` if using SQLite
- `/data/sessions/`
- `/data/jobs/`
- `/data/auth/` if you intentionally keep reusable auth state
- `/data/audit/`

## Cleanup

The controller can now prune stale artifacts/uploads/auth-state automatically.

Manual run:

```bash
curl -s http://127.0.0.1:8000/maintenance/cleanup \
  -X POST \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H 'X-Operator-Id: ops' | jq
```

## Credential handoff checklist

Before live debugging, gather:

- OpenAI / Anthropic / Gemini API keys, or populated CLI auth caches under `data/cli-home`
- gateway credentials (Cloudflare Access or Tailscale)
- bastion SSH details if using reverse tunnels
- operator identity convention (`X-Operator-Id` values)
- allowlisted target hosts/domains

## First live-debug session

1. Bring the stack up privately
2. Verify `/readyz`
3. Verify `/metrics` (unless `METRICS_ENABLED=false`)
4. Create one session against a non-sensitive site
5. Verify observe/click/type flow
6. Add real creds
7. Test one real target workflow with human takeover ready
