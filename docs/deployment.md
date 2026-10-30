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

Saving a session's state as a profile reads the page's title first, then writes the new state and
metadata beside the profile's own and puts them in place together: a save that fails or is cancelled
before they are in place leaves the profile's previous state and metadata as they were, and a first save
that fails then leaves neither. Once they are in place the save stands: a failure after that (writing
the session's `actions.jsonl`, the audit event or the witness receipt, say) is reported as an error, and
the new profile stays. A first save, and a re-save that names an owner the profile's metadata does not
(an operator's save over a profile saved under the shared token or before owners), puts the metadata,
which names the owner, in place before the state; any other re-save puts the state first. So under named
credentials a failed or killed save never leaves a new login with no owner. If a save fails and its
state cannot be put back as it was (removed, on a first save; a file Windows keeps locked, say), the new
state stays and an error is logged, naming the hidden file that keeps the previous state, if there was
one: after a state-first re-save the new state stays beside the previous metadata, and otherwise beside
the new metadata, which names the owner. When the state is put back, a save that put its metadata first
puts the previous metadata back too (none, on a first save); if that fails, an error says the new
metadata names the saving operator as owner of the previous login. A process killed during a save can
leave the new state beside the previous metadata, or the new metadata beside the previous state or none,
and hidden files beside them whose names start with `.state.json` or `..state.json` and end in
`.tmp.json`, `.tmp.json.enc` or `.tmp`, holding the previous login and, encrypted when encryption is on,
the new one; exports leave those out. A profile with metadata and no state answers "No saved auth
profile" when it is loaded, and other operators are refused it: delete it or save it again. The state
file is there throughout a save, for a session opening the profile or an export meanwhile. A rename that
Windows refuses for a moment (another process has the file open) is tried again. An encrypted save also
tries to remove the plaintext `state.json` saved before the encryption key was set; a removal that fails
is logged, and the save stands.

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

## What ALLOWED_HOSTS refuses, and what it does not

`ALLOWED_HOSTS` names the hosts the browser's top-level page may visit. Iframes and subresources an
allowed page loads from other hosts are not filtered, by design. These are the rules as of 1.11.2.

An internationalized host matches only in its `xn--` (punycode) form, so list it as `xn--...` and
navigate to its `xn--` URL. The check before a navigation reads the host as the URL writes it, in
Unicode, and the check after the load reads the URL Chromium reports, in punycode: a Unicode entry
passes the first and is refused after the load, and a Unicode URL is refused before the request by an
`xn--` entry. This predates 1.11.2 and fails closed: such a page is refused, not read.

### Stopped before the request is sent

With `PRE_REQUEST_BLOCKING=true` (the default) the controller holds each top-level document request of its
own sessions in Chromium (DevTools `Fetch`, one session per browser) and fails the one whose URL
`ALLOWED_HOSTS` refuses, before Chromium sends it. There is nothing to stop, and no gate, when
`ALLOWED_HOSTS` is empty or `*`. The promise:

- **Stopped before it is sent:** a top-level navigation's request to a host off `ALLOWED_HOSTS`, including
  every redirect hop and a popup's first request.
- **Still caught after the fact by NAV-07** (the guard that sends a refused page back to `about:blank` once it
  commits):
  - speculation-rules prefetch in attach modes, and in a launched browser where it could not be turned off
    (launched browsers turn it off when the controller acquires them; `/readiness` warns when that failed);
  - a navigation a service worker serves (one a same-site refused iframe registered) for a popup's first
    navigation, and for any the popup makes before its service-worker bypass is set (a background task once
    the controller sees the popup), in every mode;
  - `blob:` navigations a refused iframe makes;
  - bfcache restores in attach modes;
  - requests held when the gate detaches, and any navigation a page starts itself before the replacement gate
    attaches. They go out. The controller attaches the replacement before it closes that browser's sessions,
    so the window is one attach, and it refuses their actions meanwhile.
- **Any tab, rarely:** a navigation to a refused host fails closed in any tab of a gated browser, the
  operator's own included in attach modes, when the gate cannot classify its frame (its lookup failed).
- **Not covered:** iframes and subresources of other hosts.

What the agent sees:

- When the gate stops the navigation of `navigate`, a session's `start_url` (also when the page leaves for
  a refused host while it settles, by a meta refresh or a script), a shadow browse or a tab opened onto a URL,
  they answer `browser_action_blocked` (REST 403, `retryable: false`), with the message and `url` naming only
  the refused origin. A cross-origin POST redirect stopped in an attach mode (below) answers the same code with
  what to change. In 1.11.2 a `navigate` redirected into a refused host already answered
  `browser_action_blocked` ("Action blocked by policy"), and a `start_url` or a new tab's URL answered
  `not_permitted` (REST 403 `{"detail": "Not permitted"}`). Which layer refuses decides the code:
  - a URL `ALLOWED_HOSTS` refuses as written is refused before anything exists or is sent, as before (a 403);
  - a navigation the gate stops answers `browser_action_blocked`, as above;
  - a page NAV-07 refuses once it has committed (one of the gaps above, or `PRE_REQUEST_BLOCKING=false`)
    answers as in 1.11.2: `browser_action_blocked` for `navigate`, `not_permitted` for a `start_url` or a new
    tab's URL.
- Only the gate's own failure of the page's request (`net::ERR_BLOCKED_BY_CLIENT`) counts: an action that fails
  for another reason (a selector timeout) keeps its own error even if the page tried a refused navigation
  meanwhile.
- An action that did its work and whose page was then stopped (a click on a link) succeeds, and its result
  carries `target.blocked_navigations` (origin, reason, explanation) for blocks on the action's own page only;
  a background tab's block is not pinned on it.
- A tab whose navigation was stopped shows Chrome's error page, and the tab list marks it with
  `blocked_navigation` (the origin of the stopped request, the reason and an explanation), decided on the URL
  it lists. While the active page is that error page, `observe`, `browser.get_html` and an action result's
  `after` observation (so the `actions.jsonl` record and the witness receipt too) carry the same label; the
  built-in agent loop's prompt keeps it. The label goes once a later request of the page continues. The error
  page's DOM is returned as any page's, unchanged: it carries the refused URL in full, path and query included,
  since the page chose that URL.
- Each block is audited as `navigation_blocked` with `source: "request"` and the origin only.
- A browser the gate cannot attach to gets no sessions: creating one answers `pre_request_gate_unavailable`
  (503, retryable), naming `PRE_REQUEST_BLOCKING`. If the gate detaches on its own, the controller attaches
  a replacement, then closes every session on that browser (a shared browser whose context will not close is
  disconnected, and the next session reconnects). An action on one of them meanwhile is refused before it
  starts, and the next call on one answers `session_closed` with `reason: "request_gate_lost"` (REST and MCP).
- `/readiness` reports a `pre_request_gate` check: its state (off and why, idle, attached, lost, attach
  failed), whether it is an attach mode with the switch declared, the prefetch preference read back, and counts
  of service-worker bypass failures, handler errors and blocks of frames the gate could not look up. It warns
  on a launched browser whose prefetch preference did not read back 2, and on any bypass failure or handler
  error (a service worker may then serve an owned page's navigation, which NAV-07 refuses after it loads). It
  warns at worst, so a health check never restart-loops on it.

Classification relies on a page's target existing when its first request pauses, which was measured on
Chromium 151 and is guarded by the popup tests. If a Chromium change broke that, a refused page would fall back
to NAV-07 after the fact.

In the browsers the controller launches (browser-node, `docker_ephemeral`, shadow browsing), while the gate
is on (prefetch and the bypass; partitioning always):

- speculation-rules prefetch is turned off: the controller sets `net.network_prediction_options` to 2 in the
  browser's default profile when it acquires it, whatever the gate's attach did, and reads it back. That is the
  control, measured on Chromium 151. If it cannot be set, the controller logs a warning, `/readiness` warns, a
  prefetch of a refused host is sent, and the page is refused after it loads. The browser-node image also
  installs the `NetworkPredictionOptions` managed policy at both the Chromium and the Chrome for Testing policy
  paths, as a second layer: which path that Chromium build reads is unverified. A prefetch is fetched without
  DevTools seeing it, so the refused host would get the request;
- owned pages bypass service workers (`Network.setBypassServiceWorker`), so a worker cannot answer their
  navigations without a request the gate sees (except a popup's first navigation, which starts before the
  controller sees the popup, and any it makes before its bypass is set; NAV-07 refuses those after they load);
- third-party storage partitioning is back on, as in Chrome: Playwright turns it off, and the controller passes
  Playwright's own `--disable-features` list without `ThirdPartyStoragePartitioning`. What changes for sites:
  a third-party iframe's `localStorage` is keyed to the top-level site, so the same origin opened top-level
  does not see it, and a saved auth profile (`storage_state`) does not carry it. Its cookies and first-party
  logins round-trip as before.

In the attach modes (`CDP_CONNECT_URL`, `browser.cdp_attach`) the gate stops navigations only in the contexts the
controller creates (apart from a frame it cannot classify, above), and the operator's prefetch setting is left
alone. Every navigation in that Chrome, the operator's own tabs included, waits on the gate's reply: about 13 ms
each (the median measured on Chromium 151). Start that Chrome with
`--disable-features=BlockOriginHeaderModificationOnRedirect` and set `CDP_BLOCK_ORIGIN_ON_REDIRECT_DISABLED=true`:
without the switch, Chrome fails a cross-origin 307/308 POST redirect hop that a DevTools `Fetch` handler lets
through, so the controller stops such a hop in its sessions itself, with an error naming the switch, and the
same redirects fail in the operator's own tabs too while the gate is attached. The controller logs a warning,
and `/readiness` warns, while the switch is not declared.

`PRE_REQUEST_BLOCKING=false` turns the gate off; NAV-07 then refuses such pages only after their request has
gone out, as before.

### When a page is refused

A page is refused while it is on a host `ALLOWED_HOSTS` does not allow, and while it is tainted. It is
tainted when it commits a refused URL; when it commits a page its navigation reached through a refused
host (a redirect from one to an allowed URL); when it commits any page on its own while another page of
the session is refused (a refused page can send the tab that opened it to `about:blank` and write into
it, or to an allowed URL whose path and query carry what it read); and when it is a popup whose opener
is refused or tainted, whatever the popup's URL, or whose first navigation passed a refused redirect. It
is refused, too, while its opener is still being looked up. A popup or blank window a refused page
opened is refused with it. A page on a `blob:` URL is judged by the origin that made it; one with no
origin (`blob:null`) is judged by where it came from alone. A page refused only while it navigates
through a refused host is read again once that navigation fails without loading a page (a `204`, or a
download). Every check below that judges a page uses this rule.

When the controller attaches its listeners to a page (a session's first page, a popup, a new tab), it
checks the URL the page is already on, as it does after a later navigation. A refused top-level page,
a tainted one included, is sent back to `about:blank` with a `navigation_blocked` audit event.
Shadow-browse sessions attach these listeners to every popup.

### Reading a refused page again

Observe, screenshots, the page-reading MCP tools and actions other than navigation answer that a
refused page is not read (REST: 403), also after the controller has sent it back to `about:blank`, and
after the page has loaded an allowed URL on its own (a refused host's redirect, a script). The taint
clears only in two ways:

- the agent navigates the page to an allowed URL, and that navigation returns a response that ends on an
  allowed http(s) URL, no hop of its redirect chain was refused, no commit during it came from a refused
  hop, no commit after its own was made while another tab of the session was refused (that tab can send
  it on before the navigation returns, so a page that moves itself while it loads, by a client redirect
  or `history.replaceState`, stays refused then), and no refused hop is still in flight when it returns;
- a blocked action's rollback brings the page back to the page the agent read before the action, with no
  refused hop on the way and no other tab of the session refused meanwhile.

The page's own navigation to an allowed URL does not clear it, nor does the controller's own blank,
since a refused opener can write into a later blank of its window. A navigation within the page (to a
`#fragment`) returns no response and clears nothing. `go_back` runs on a refused page but leaves it
refused; `reload` and `go_forward` are refused there. A refused tab that is not the active one cannot be
activated, and so cannot be navigated: close it, or open a new tab. The REST `403` says so.

The TOTP autofill skips a refused page, also on a host in `totp_hosts`, since a refused host's redirect
can choose that URL. Forking a session onto its page's URL (no `start_url`) refuses while its page is
refused, and over REST answers the same 403 as observe (any other refusal of a fork answers the generic
403); a fork onto a `start_url` the caller names is judged by that URL, as a new session is. Shadow browse
from a refused page answers that 403 too, and from a `data:` or `blob:` page the generic one. From Chrome's
error page, a fork without `start_url` and a shadow browse answer 400 with a fixed text: the page is Chrome's
error page for a navigation that was stopped or failed; fork's says to pass `start_url`.

### Other tabs while one is refused

While a tab of the session counts as refused, the other tabs' own navigations are refused too: a refused
tab can send another tab it holds (the one that opened it, say) to an allowed URL whose path and query
carry what it read. A tab that loads a page on its own meanwhile (a link the agent clicks, a redirect, a
script, a reload or go back) is refused until the agent navigates it. The controller sends it back to
`about:blank`, with a `navigation_blocked` audit event that names its (allowed) origin, and whatever it
had loaded, a form in progress included, is lost. Close the refused tab if it is still open, then, with
the other one active, navigate it to an allowed URL; the refusal says so. Each of the agent's actions on
the session (a navigate, a click, typing, a key press, a history move), and a tab it opens onto a URL,
first waits out what is left of the 2 seconds after a refused tab closed (below), so what the page
commits because of it (a `#fragment`, `history.replaceState`, a client redirect) is not taken for the
closed tab's choice. The wait lasts at most 2 seconds from the call, however many refused tabs close
meanwhile (a navigation that goes on while a later close's 2 seconds still run is refused), and there is
none while a refused tab is still open, where it would change nothing. It holds the session, and comes
before the action's snapshot and before the action begins: a call cancelled meanwhile gives a claimed
approval back. A tab refused this way that is not the active one stays refused after the refused tab
closes, and cannot be activated: close it too, or open a new tab. Activating it is refused (REST: 403)
with "That tab loaded while another tab of this session was refused, or within 2 seconds after a refused
tab closed, which can send it to a URL it chose, and cannot be activated. Close it, or open a new tab."
A tab's own navigation that starts, or a blank, `blob:` or `data:` page that commits, within 2 seconds
of a refused tab closing is refused like one made while it was open, and so is one that started while it
was open; the agent's navigate clears it. The agent's own actions wait those 2 seconds out first
(above), so this is a page's own navigation, or an action that went on past its wait.

The tabs that count are those on a host `ALLOWED_HOSTS` does not allow, those that came from one (by a
redirect or their opener), those navigating through one, and those that loaded a blank, `blob:` or
`data:` page while another tab was refused, since such a page has the origin of the page that made it. A
tab refused only because another tab was refused when it loaded an allowed http(s) page, or one still
waiting for the page that opened it to judge it, stays refused itself but does not count while it is on
an allowed http(s) page, whose document comes from that URL's origin, or on the `about:blank` the
controller sent it to, whose origin is its own; on any other blank, `blob:` or `data:` page it counts.

### Logs of a refused page

Console messages and page errors are not kept while the list refuses their page.

The network log withholds a request made by a refused page, and one that navigates a top-level page to
a refused host. Such a request is recorded with `"withheld": "page off ALLOWED_HOSTS"`, its URL cut to
the origin (`scheme://host:port`), its method reduced to a standard one or `OTHER`, and its status,
resource type, timing and failure, with no headers or bodies. Network hooks receive that entry and match
its origin, also when it is logged unfinished because too many requests were in flight. Which page made
a request is decided when it starts. These are withheld too, whatever their URL:

- a popup's first navigation, since its page cannot be judged yet and a refused opener may have chosen it;
- any top-level navigation that starts while a page of the session counts as refused for the others
  (above), a navigation from a refused page to an allowed URL among them;
- a service worker's request while such a page is open, or when its script's URL is refused;
- any other request with no frame, which comes from a page the browser has not reported yet: a window a
  refused page opened onto a `204` is never reported, and the refused page can script its blank document;
- a request that cannot be attributed.

`request_failures`, in every observation and `browser.get_request_failures`, leaves a failed request out
when its page was refused as it started (so one that fails after its page was blanked, or after the page
loaded an allowed URL, is left out too), when its page is refused as it fails, and when it is a top-level
navigation to a URL off the list.

### Refused downloads

A file the browser downloads must come from a host on `ALLOWED_HOSTS`, from a page on one. The host
checked is the one the file comes from after its redirects: Playwright reports a download's final URL,
so a file an allowed host redirects to (a CDN, a file host) is refused unless that host is on the list
too, and listing it also lets pages navigate there. A file a page generates (a `blob:` or `data:` URL)
is judged by the page that reports the download, not by the origin that built it, so one that an iframe
from another host builds on an allowed page is kept, and one a refused page generates is refused. A
download a navigation through a refused host turned into is refused too, whichever of the download and
that navigation's failure the browser reports first. That includes a popup's first navigation, whose
download Playwright reports on the page that opened the popup; such a download's URL stays refused for
every download of the session, the last eight such URLs kept, since no page holds it for the agent's
navigate to clear.

A refused download is cancelled. A failed cancel is logged (at debug level), and nothing is saved either
way. It is recorded in the session's downloads and `downloads.jsonl` with status `blocked`, no file name,
path or artifact URL, its source URL's origin only and the reason, and a `download_blocked` audit event
is written; `browser.read_download` refuses it as not completed.

### Cookies an allowed host would receive

`browser.get_cookies` returns a host-only cookie when its host is allowed, and a domain cookie when its
domain is allowed or is a parent of an allowed host (`Domain=example.com` is returned when only
`app.example.com` is listed, as the browser sends it there). A partitioned cookie, which the browser
sends only under the top-level site it is keyed to, is dropped when no listed host belongs to that site
(an IPv6 site is judged by its bracketed address). A URL in `urls` that the list refuses is dropped
before the browser is asked, so naming only refused URLs returns no cookies. With `ALLOWED_HOSTS` empty
or `*`, nothing is filtered.

### Reads and the page tools

Each read notes the session's active page and that page's count of refused commits before it reads, and
refuses afterwards if either changed, the page closed, or the page is refused now; a page that went from
one allowed URL to another meanwhile is read. This covers observe, every screenshot (deleted when
refused, and when the read is cancelled before it is checked), the snapshots around an action, the share
page's frame and title (the share poll answers 403), `browser.eval_js`, `browser.wait_for_selector`,
`browser.get_html`, `browser.find_elements`, the storage tools (refused before a write and checked after
it), `browser.drag_drop`, the DOM resource and the CDP routes. Tab listings, session summaries, approval
requests and saved auth profiles leave the title empty in the same cases, and a bot challenge found on a
page that moved while it was probed is refused instead of described.

A refused page's URL is shown by its origin only (`scheme://host:port`) in tab listings, session
summaries, the snapshots an action returns, approval requests and the `navigation_blocked` audit event.
`about:blank` is shown whole, and so is the URL of a page that is allowed again by the time it is shown.

The page-reading MCP tools (`browser.eval_js`, `browser.get_html`, `browser.find_elements`,
`browser.get_local_storage` and `browser.set_local_storage`, for local or session storage,
`browser.drag_drop` and the `browser://<session>/dom` resource) take the session lock: they wait up to
10 seconds for a session another call holds and then fail, and give up on a page that does not answer
within 30 seconds (a `browser.find_elements` text query within 10, `query_timeout`; a drag: 30 seconds,
plus `ACTION_TIMEOUT_MS` for each end named by a selector or `element_id`, the most that end waits for
its element). `browser.eval_js` takes an optional `timeout_ms` (default 30000, at most 60000) in place
of those 30 seconds, and its timeout error says the expression may still be running in the page, which
it may: giving up does not stop the page's script. `browser.wait_for_selector` does not take the lock,
so an action can run while it waits (the click that makes the element appear, say); it keeps its own
`timeout_ms`. It is refused if, while it waited, its page went to a refused host or became refused,
closed, or stopped being the active tab, and that refusal comes at its `timeout_ms` plus 250 ms, whether
or not the selector matched.

A drag whose mouse button may have gone down sends one release, whether the drag succeeded, failed or
reached its deadline, and waits at most 5 seconds more for it, still holding the session, also when
the call is cancelled meanwhile. A release the browser does not answer in that time fails a drag that
had succeeded; after a drag that failed, the drag's own error is the one reported. A drag cut off while
it brought an end named by `element_id` into view also waits at most 2 seconds, before the release, to
remove its scroll watch.

The REST CDP routes, `GET /sessions/{id}/cdp/element` and `POST /sessions/{id}/cdp/raw`, take the
session lock and answer 403, as observe does, while the list refuses the session's active page or when
it moved during the call. When they cannot attach to the active tab they answer 503 rather than read
the tab they were attached to before. A session another call keeps for more than 10 seconds gets 409. A
command the page does not answer within 10 seconds gets 504 and lets go of the session, and so does an
attach to the active tab, or a detach from the tab before it, that the browser does not finish within 10
seconds.

### What a refused page still does

- **Its request has already gone out when it is refused.** A top-level navigation the pre-request gate
  stops never reaches the host ([above](#stopped-before-the-request-is-sent)), so a refused page that
  commits got past it: one of the gaps listed there, or `PRE_REQUEST_BLOCKING=false`. NAV-07's checks run
  when a request starts or a navigation commits, and that navigation request, with whatever the page put in
  its URL, reached the refused host. The page runs, sets cookies and shows in the takeover (noVNC) view
  until it is blanked.
- **Blanking waits for any call in flight on the session to finish.** The page-reading MCP tools hold
  the session at most 30 seconds each (`browser.eval_js` up to its `timeout_ms`), a drag also
  `ACTION_TIMEOUT_MS` for each end named by a selector or `element_id`, and 7 seconds more: 2 to remove
  its scroll watch and 5 for its release (`browser.wait_for_selector` does not hold it). Observe and
  actions make several page calls in turn and are not bounded as a whole, so they can hold it longer. A
  service worker the page registered keeps running after that.
- **What an allowed origin's own scripts do with what a refused redirect or a refused opener put in its
  URL is not tracked.** They can keep it in that origin's storage or cookies, send it to that origin's
  server, or pass it to its other windows and service worker, and reads of those after the page is
  readable again return it. A tab refused only because another tab was refused when it loaded an allowed
  http(s) page runs such scripts too, and does not refuse the others while it stays on that page or on
  the `about:blank` the controller sent it to. Two cases of this: a service worker's requests are
  withheld while a page of the session counts as refused or when its script's URL is refused, so a worker
  on an allowed origin can keep what a refused page sent it and send it once no such page is open, and
  that request is logged whole; and a page on an allowed origin that a refused host's redirect or a
  refused opener reached is refused, but it can write into another window of its origin that it holds
  (through `window.opener`, say), and that window is not refused, and the agent reads what it wrote
  there.
- **Cookies.** A refused page can set a cookie on a parent domain that an allowed host receives
  (`evil.example.com` setting `Domain=example.com` when `app.example.com` is listed), and
  `browser.get_cookies` returns it, as the browser sends it to the allowed host. Partition keys are
  judged by site in the same way, so a partitioned cookie a refused host set under its site is returned
  when an allowed host belongs to that site. A glob entry is matched against a cookie's domain by its
  text, so an entry such as `*.example.*` can withhold a domain cookie that a host it matches would
  receive.
- **Logs.** A withheld network entry keeps the host's labels, which the page chooses, and its timing,
  status and resource type. A popup's first navigation is withheld even when an allowed page opened it.
  A request still in flight when its page navigates away is not listed until the log evicts it, since
  Playwright reports no failure for it. A console message, page error or download is judged as its page
  is when it is delivered, so one a refused document produced that is delivered only after the agent's
  navigation has made its page readable again is kept (a page's own navigation to an allowed URL leaves
  it refused).
- **Tabs.** A tab still waiting for the page that opened it to judge it (that page closed first) stays
  refused until the agent navigates it or it is closed. On an allowed http(s) URL it does not refuse the
  other tabs; on a blank, `blob:` or `data:` page, which can hold what that page wrote, it does. An
  iframe from another host on an allowed page can open a popup, which is judged by its opener, the
  allowed page. A popup is judged by its opener as the opener is when Playwright reports the popup,
  after its first commit, so one a refused page opened just before the agent's navigate made that page
  readable again is readable too. A popup the context has not reported yet does not count for the
  session, though a refused host's document may already run in it, so a tab it navigates in that moment
  is not refused for it; and the refused first navigation of a popup that closes before it is reported
  stays on the session's record (a download from its URL is refused) until the session ends. The 2
  seconds after a refused tab closes are measured by the clock: a commit that comes with no request (a
  `#fragment`, `about:blank`) and that a slow renderer delivers later than that is not refused for that
  tab.
- **Traces and exports.** Playwright traces (`ENABLE_TRACING`, off by default) record the whole browser
  context, the refused page included, and a storage-state export (`POST /sessions/{id}/storage-state`,
  saving an auth profile) holds every host's cookies.
- **Iframes and subresources** from other hosts on an allowed page are not filtered, by design.

## Witness receipts: what a signature proves

Each receipt is hash-chained to the one before it and signed with an Ed25519 key. The key is generated on first use in `<WITNESS_ROOT>/keys`, beside the receipts it signs. That decides what the signature can prove:

- **It proves the receipts are consistent and were signed by that key.** `GET /sessions/{id}/witness/verify` and the `browser.verify_witness` tool report this: the hash walk, the anchor, and a `signatures` block. A chain with no signed receipt fails the `signatures` check, and so does an unsigned receipt after a signed one. The top-level `valid` passes only when all three do. `chain_valid` is the hash walk and the anchor alone, and `reason` says which check failed. Missing evidence is never `valid`: a chain with no receipts (deleted, or never written), a chain whose anchor file is missing or unreadable (`anchor.status` is `anchor_missing` or `anchor_unreadable`), and a chain shorter than its anchor all fail. An id with no session record, no chain and no anchor answers `404`, for verify and for the bundle. Under named credentials (`API_BEARER_TOKENS`) an id with no session record answers `404` even with a chain, since no owner can be checked for it: the chain of the auth-profile export and import receipts (`auth-profiles`) is one, so its witness routes and tools answer `404` to every operator. Chains written before 1.7.0, when anchors arrived, are anchored as they are on the first start after upgrading (the root is then marked `.anchors-migrated` and no later start anchors anything), so their hash walk and anchor keep passing, though a chain written wholly before 1.6.0, when receipts were first signed, still fails the `signatures` check; anything lost from them earlier cannot be detected. That start anchors a chain of any release that lacks its anchor, so a chain whose anchor was deleted on 1.11.1 or earlier is anchored as it stands too: if its evidence matters, check the chains without a `<id>.jsonl.anchor.json` before the first 1.11.2 start. A later receipt never moves the anchor over a chain that was cut short or lost its anchor: the chain stays flagged, and the receipt is still recorded. With `WITNESS_ENABLED=false` no session receipts are recorded (auth-profile export and import still record theirs), so a session that has none verifies `false`; receipts recorded earlier still verify. Anyone who can write the witness directory can still make a cut chain verify: by rewriting the anchor; by deleting the chain together with its anchor, after which the next receipt starts a new chain that verifies; or by deleting `.anchors-migrated` before a restart, which anchors a cut chain whose anchor is missing as it stands. For evidence that must survive that, pin a head hash off the box (below). A crash or a full disk between a scope's first receipt and its anchor leaves that chain `anchor_missing` for good. A receipt whose anchor update failed leaves the chain one receipt ahead of its anchor. Until the next successful append moves the anchor over it, verify reports the chain `diverged` (`valid: false`, "the chain was rewritten or forked"), and that receipt can be removed without verify noticing. If no receipt follows (it was the scope's last), the chain stays `diverged`. To recover a chain you have checked by other means, delete `<WITNESS_ROOT>/.anchors-migrated` and restart: that anchors every chain that has no anchor file, as it stands, so it also anchors any cut chain whose anchor was deleted. A chain whose anchor file is still there (unreadable, truncated, or diverged, such as one whose last anchor update failed) is skipped: delete its `<id>.jsonl.anchor.json` as well.
- **It does not prove anything against someone who controls the box.** Whoever can write the witness directory can also read the key, so they can edit receipts and sign the result. A bundle that verifies tells a recipient the receipts match the key in the bundle, not that the key was never in the wrong hands.

How verify reads a chain, since 1.11.2: `valid` is `false` when the chain file is gone (even with its anchor left behind), when a chain has no receipts, when its anchor file is missing (`anchor_missing`), and when the anchor is corrupt, not UTF-8 or malformed (`anchor_unreadable`: the count must be an integer of at least 1 and the head a non-empty string). The chain's length and head must match the anchor exactly; `truncated` and `diverged` fail as well. The `anchor.status` value `unanchored`, which passed, is gone. A final receipt that parses but lost its newline counts as a receipt in verify, list, export and the next append, which completes it instead of setting it aside. A chain with an unparseable record fails the signature check with a verdict, not an error. `chain_valid_at_export` in a witness bundle follows the same rule, and `scripts/verify_witness_bundle.py` fails a bundle with no receipts.

For evidence that survives a compromised controller host, keep a head hash from before the compromise and check it when you verify: `python scripts/verify_witness_bundle.py bundle.json --expect-head <head_hash> --expect-key-id <key_id>`. The `head_hash` and `signing_key_id` come from `GET /sessions/{id}/witness/bundle`; the verify result has the `head_hash` too. Sending a copy of each receipt to a hosted Witness service (`WITNESS_REMOTE_URL`), or copying the key id and head somewhere the controller host cannot write, works as well. Without such a pin, a recipient can only check that the bundle is internally consistent.

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
- `/data/witness/`, whole: the chains with their `*.anchor.json` files, `.anchors-migrated` and `keys/` (since 1.11.2 a chain restored without its anchor fails verification, and receipts verify only against the key that signed them)
- `/data/crons/` if you use cron jobs
- `/data/approvals/` if pending approvals and operators' rejections should survive a restore; a copy taken between an approve and its claim brings that approval back as `approved` until it expires (`APPROVAL_TTL_MINUTES` after the approve), so after restoring one, reject any whose action already ran

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
