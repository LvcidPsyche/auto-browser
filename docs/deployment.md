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

Downloads a page starts are capped by `DOWNLOAD_MAX_BYTES` (default 256 MiB; a larger file is deleted) and
`DOWNLOADS_MAX_PER_SESSION` (default 100; the rest are cancelled). Both are recorded as `refused`.

`CONTROLLER_ALLOWED_HOSTS` protects the controller itself from unexpected HTTP Host headers.
Keep `ALLOWED_HOSTS` for browser navigation targets, and set `CONTROLLER_ALLOWED_HOSTS`
to the hostnames operators use to reach the controller, such as `browser.example.com`
or `localhost,127.0.0.1,::1` for a local-only install. An entry is an exact name, an
IPv6 address with or without brackets (`[::1]`, `::1`), a suffix such as `*.example.com`,
or `*`. Brackets round anything else (`[localhost]`, `[*]`) and a bare `*.` stop the
controller at startup.

When it is unset and the controller runs without a bearer credential (a loopback
deployment with `API_ALLOW_TOKENLESS_LOOPBACK=true`), only loopback Host names —
`localhost`, `127.0.0.1`, `[::1]` — are answered. An unauthenticated API that accepts
any Host header can be driven by a web page through DNS rebinding, so this default
holds until you either list the exact names clients use or let the controller use a
token. `/healthz` is exempt.

On a controller without a bearer credential, a wildcard (`*`, `*.example.com`) admits
no Host: anyone who can get a name under it can point that name at 127.0.0.1, and a
page served from it would then drive the controller whole, with no token to ask for.
Such a controller does not start with a wildcard in the list, and says what works
instead: a token, or exact host names. An exact name is only as safe as its DNS:
list only names whose DNS you control, or run with a token. A single-label name such as
`controller` can be answered by another host on the same network (mDNS, LLMNR, a DHCP
search domain). A controller with a token admits by its wildcards as before.

## API token

Every API and MCP request carries `Authorization: Bearer <token>`. `API_BEARER_TOKEN`
(or named `API_BEARER_TOKENS`) sets it. Without either, the controller uses the token in
`API_TOKEN_FILE` (`/data/api-token`, which the shipped compose maps to `./data/api-token`
on the host), and a loopback controller outside production generates one there on first
start: 0600, and given to the data directory's owner when the controller runs as root
(the shipped image does), so host clients can read it.
An exposed or production controller never generates one; it refuses to start without a
token, from either source.

- Clients read the file: the stdio bridge with `--token-file` /
  `AUTO_BROWSER_TOKEN_FILE`, the SDK and LangChain adapters with `token_file`, and the
  repo scripts (`make doctor`, the smoke tests) by themselves.
- With a custom compose file and a named volume, read it with
  `docker compose exec controller cat /data/api-token`.
- A controller run outside compose defaults to `/data/api-token` too. On Windows
  that is `C:\data\api-token`, which inherits permissions that let every local user
  read and replace it, so set `API_TOKEN_FILE` to a path under your user profile.
- On POSIX a token file (this one or `TAKEOVER_TOKEN_FILE`) that group or others can read
  is changed to 0600 at startup. A symlinked one (a Kubernetes secret mount, 0644 by
  default) is used as it is, its target's mode untouched, with one warning naming the
  path and mode when that target is readable beyond its owner: set the mount's mode
  (`defaultMode: 0400`) instead.
- To rotate the generated token, delete the file and restart the controller. Once
  `API_BEARER_TOKEN` is set the file is ignored (the startup log says so), and
  clients still pointed at it get a 401.
- `API_ALLOW_TOKENLESS_LOOPBACK=true` serves a loopback controller with no token.
  Any process that can reach port 8000, containers on the compose network
  included, can then drive the browser. It never applies to an exposed or
  production controller. Such a controller refuses (403) a POST, PUT, PATCH or DELETE that a browser marks as
  `Sec-Fetch-Site: cross-site` or `same-site`, or whose `Origin` is not the host and port in its `Host` header: a web
  page on another origin, or on another port of the same host, must be served from the controller's own origin. A
  token does not help a page (nothing checks one on such a controller, and no route answers a CORS preflight, so a
  page on another origin cannot send one to a controller that has a token either); a browser extension, or a
  non-browser client that names an origin, needs the controller to have a token, and sends it.
  `GET /auth-profiles/{name}/export`, which writes an archive, is refused the same way when a browser marks it so.
  A request with neither header (curl, an SDK) is served. `/mesh/receive` is signed, and `/mcp` keeps its own Origin
  check and `MCP_ALLOWED_ORIGINS`. A loopback reverse proxy in front of such a controller must pass the browser's
  `Host` through with its port, or the controller needs a token: a proxy that rewrites `Host`, or listens on a port
  other than the scheme's default and forwards the name without it, leaves the browser's `Origin` naming another host
  or port, so every write from the dashboard it serves is refused. In nginx, use `proxy_set_header Host $http_host;`:
  `$host` drops the port.
- `/metrics` needs the token too: give a Prometheus scraper `authorization: { type: Bearer, credentials_file: <path> }`.

## Chromium's sandbox (BROWSER_SANDBOX)

Chromium's own sandbox puts each renderer in a user namespace (with PID and network namespaces) and under a seccomp
filter, so a renderer exploit does not start with every syscall the container allows. Docker's default seccomp profile
refuses those namespaces without CAP_SYS_ADMIN, which browser containers drop, so compose and the controller apply
`controller/app/browser/seccomp/chromium-sandbox.json`: moby's default profile plus one entry allowing `chroot`,
`clone` and `unshare` (provenance and the refresh procedure are in the README beside it). That lets code in the
container create user namespaces, which exposes the kernel's user-namespace code to it: renderers cannot reach it
(Chromium's own filter blocks them), but the browser and network-service processes, which Chromium does not filter,
can. In exchange every renderer runs sandboxed, which is the better trade for a browser that renders arbitrary pages.

| `BROWSER_SANDBOX` | What happens |
|---|---|
| `auto` (default) | Chromium starts sandboxed, and browser-node checks in `/proc` that each renderer is in its own user namespace under one more seccomp filter. A failure is retried once; then Chromium runs unsandboxed and a WARNING names the cause. |
| `on` | Nothing the controller launches runs unsandboxed. A browser that cannot run sandboxed does not start (a sandboxed launch that fails is retried once first): browser-node exits, and an isolated session fails. The controller enforces it too: an isolated container whose state is not a verified `on` (an image older than 1.14.0, which ignores the setting, included) is refused and removed, and in shared mode every session is refused, and readiness fails, while the shared browser-node's state is not a verified `on` (`/readyz` is unchanged). With `docker_ephemeral`, an unreadable profile file, or a daemon without seccomp, refuses every session. `browser.cdp_attach` is refused; a `CDP_CONNECT_URL` Chrome is the operator's and is not checked (readiness warns: attached, not checked). Shadow browse (`SHADOW_BROWSE_ENABLED`) launches its local Chromium with the sandbox required, so it does not start without one, but the controller does not verify it. |
| `off` | Chromium runs unsandboxed (the behaviour before 1.14.0). `docker_ephemeral` containers get no custom profile, only Docker's default. |

The causes a fallback names are distinct, so a setup problem never reads like a host restriction:

- `seccomp_profile_not_applied`: the container runs under Docker's default profile (a plain `docker run`, or
  `BROWSER_SECCOMP_PROFILE=builtin`). On a host that also restricts user namespaces with AppArmor, the detail names
  both possibilities, since the profile hint is only what the container was told;
- `userns_refused`: the profile allows it, but the kernel or AppArmor refused the user namespace. Expected on Ubuntu
  23.10 and later (`kernel.apparmor_restrict_unprivileged_userns=1`), where `auto` runs unsandboxed and says so;
- `seccomp_unsupported`: the Docker daemon reports no seccomp support (`docker info`), so no profile was sent;
- `verification_unsupported`: the kernel has no `Seccomp_filters` in `/proc/<pid>/status` (Linux before 5.9) and
  `chrome://sandbox` did not confirm the seccomp-BPF sandbox. On those kernels the check uses `chrome://sandbox`;
  5.9 or later is the supported minimum for the `/proc` evidence;
- `chroot_refused`, `chromium_refused`, `not_verified`: the namespace works but Chromium or the check did not.
- `launch_timeout`: a sandboxed launch did not finish within the sandbox phase budget.

The sandboxed phase, both attempts with their launches and checks, has one 30 s budget: a launch that hangs is a
failed attempt, and a second attempt starts only with at least 10 s of it left. The unsandboxed fallback launch then
gets 10 s more. Keep `ISOLATED_BROWSER_WAIT_TIMEOUT_SECONDS` above that budget plus 10 s (the default, 45 s, is).

browser-node prints the outcome (`chromium sandbox: on (verified)`, or the WARNING or refusal) to its log, which is
`docker compose logs browser-node` (and `/tmp/playwright-server.log` in the container). It also writes the state to
`browser-sandbox.json` beside its endpoint file. `GET /readiness` reports them in the `browser_sandbox` check, one
line per browser (the shared browser-node and every live isolated container): it passes when every browser is
sandboxed, warns when one fell back (naming the browser, the cause and the detail), and fails when one refused to
start or when `BROWSER_SANDBOX=on` and one is not verified `on` (renderers in their own user namespace, with one
/proc filter count per renderer, each above the browser's, or chrome://sandbox's report). The state is
self-attested by the container it describes, so it is worth what that container was worth when it wrote it: it is
written before the browser loads any page but its own check, and is trustworthy only until the browser runs
untrusted content. The controller reads it without following links, from a regular file of at most 16 KiB, keeps
only known keys and strips control characters. An isolated container's is read once, when it is provisioned. The
shared node's is a snapshot taken when the controller connects to it, reported only while that connection lasts,
and reused, never re-read, when the controller reconnects to the same endpoint; a new endpoint (a new run) is read
afresh. The state names the endpoint of the run it describes, and the controller takes it only when that is the
endpoint it connected to; otherwise it is `unknown` (a browser-node rolled back to an image that writes no state
leaves the previous run's `on` beside its new endpoint). A browser compromised later can rewrite both files, but not
what the controller already holds; a controller restart reads them again, and so does a reconnect to a new endpoint
(that file too is the browser user's to write). While disconnected, readiness reports the files as they are: unknown
with an endpoint file, and without one only a refused state file is reported. The endpoint file itself is read
like the state (no link, no FIFO, a short ws:// URL), and an error about it never quotes it. Reusing a snapshot
per endpoint is sound because Playwright's
launchServer mints a random path for every run, so a new run never repeats an old endpoint. In `docker_ephemeral` mode no session
runs on the shared browser-node, so readiness shows its line for information and never warns or fails on it. To see the
live evidence in a container, run
`docker compose exec -u browser browser-node node /opt/browser-node/sandbox-report.mjs` (the state, a `/proc` check
and `chrome://sandbox`).

**To run without Chromium's sandbox under compose, set both** `BROWSER_SANDBOX=off` and
`BROWSER_SECCOMP_PROFILE=builtin`. Compose cannot make the profile follow the setting, so each does part of it:
`BROWSER_SANDBOX=off` alone runs Chromium unsandboxed but leaves user namespaces allowed in browser-node, and
browser-node and readiness warn about that; `BROWSER_SECCOMP_PROFILE=builtin` alone gives browser-node Docker's
default profile, so `auto` falls back (cause `seccomp_profile_not_applied`) and `on` refuses to start. The controller
needs no second setting: under `off` it applies no profile to `docker_ephemeral` containers.

**A daemon with its own `seccomp-profile`** (daemon.json): 1.13.0 passed no seccomp option, so its containers got the
daemon's configured profile. 1.14.0's profile replaces it, and `builtin` means moby's built-in profile, not the
daemon's. To keep a hardened daemon profile, set `BROWSER_SECCOMP_PROFILE` (compose) and
`ISOLATED_BROWSER_SECCOMP_PROFILE` (the controller) to that profile plus the auto-browser entry. That path may be a
link (a Kubernetes ConfigMap mount works), but it must not sit in a directory a browser container can write
(`data/browser-profile`, `data/downloads`, `data/browser-sessions`). **A kernel without seccomp**: a custom profile
makes Docker refuse to create the container, where its default only warned. For
`docker_ephemeral` the controller checks `docker info` and sends no profile there (auto falls back, on refuses); under
compose, set `BROWSER_SECCOMP_PROFILE=builtin` on such a host.

## Who the controller runs as (CONTROLLER_RUN_AS)

The controller image needs Linux 4.3 or later: setpriv clears the ambient capability set on every start path but
`CONTROLLER_RUN_AS=root`, and older kernels have none.

The controller image's entrypoint runs as root only long enough to choose a user and hand it its files, then runs
uvicorn as that user through `setpriv`, with no capability in any set and no_new_privs (even under a plain `docker run`
without compose's `no-new-privileges`):

- the owner of `/data` when that is neither root nor browser-node's uid 10001: the user who owns `./data` in a Linux
  checkout (and CI's runner). That user keeps its files, and `data/api-token` stays theirs at 0600, so the stdio
  bridge and the scripts read it. Its group becomes the controller's primary group unless it is group 0 or a system
  group of the image, in which case the uid's own group is used;
- otherwise uid 10002, the image's `controller` user: Docker Desktop, where bind mounts show as owned by root, named
  volumes, and a data dir a 1.14 pre-release left owned by 10001. The controller never shares the browser
  containers' uid.

At start, what earlier releases left in the controller's own `/data` subtrees (entries owned by root, or by 10001
from a pre-release) is given to that user. The walk holds directory file descriptors and changes each entry relative
to its directory without following links, so swapping a directory for a symlink mid-walk cannot lead it out of the
tree; each directory is handed over after its contents, through the descriptor already open, so root never locks
itself out of a 0700 directory mid-walk. When something under a directory fails, the directory keeps its owner only
if handing it over would lock root out of it (not o+rx): the next start can then still reach and retry what failed.
`/data` itself is otherwise handed over, so one stuck entry never keeps the controller from writing into it. The walk
goes at most 200 levels below each configured path, and 2,000,000 entries in a pass (beyond either is an error), and
logs its progress every 10,000 entries. It skips every mount point under `/data`, and a configured path whose
parents include one (read from `/proc/self/mountinfo`,
so a bind of the same filesystem, or Docker Desktop's single share, is skipped too, not only another device), skips
files with more than one hard link, never enters the browser containers' directories (`browser-profile`,
`downloads`, `browser-sessions/*/profile` and `browser-sessions/*/downloads`), refuses a configured path with `.` or
`..` in it (`ARTIFACT_ROOT=/data/../etc` is not handed over), and refuses one deeper than one level whose parents are
not root-owned directories. An error on one path is logged and the others are still handed over; while any error
remains, no marker is written and one ERROR line says the pass is retried on every start until it is fixed. A
directory that is the controller user's already and that root cannot open (a 0700 one, without CAP_DAC_OVERRIDE or
CAP_DAC_READ_SEARCH) is taken as handed over without looking inside, with one WARNING per pass: after running an
older release, or `CONTROLLER_RUN_AS=root`, on this data, such a container cannot hand over what those wrote in
there, and `./data` must be chowned by hand. A pass without errors leaves `data/.auto-browser-owner`,
and later starts as the same user skip the scan; a root run (`CONTROLLER_RUN_AS=root`) removes it, deleting it forces
a new pass, and anything unexpected there (a symlink, a FIFO, another owner, other content) just means a new pass.
The marker belongs to the controller's user, who owns `/data` anyway, and root writes it as that user: that user
could forge it, but that only skips the hand-over of its own files.
Measured on Docker Desktop for 20,000 files: 0.37 s in a named volume, but 103 s once on a Windows bind mount
(`./data`), whose file operations are slow; with the marker, later starts take under a second. The controller logs
that it is starting the one-time pass, and `scripts/doctor.sh` waits up to ten minutes for the API. The startup
cleanup sweep (`CLEANUP_ON_STARTUP`) runs in the background once the API is up, logging its start and its counts
when it finishes; it protects live sessions' directories and removes only entries older than the retention period,
and shutdown cancels it. Files created later as root (`docker compose exec controller
...` runs as root unless told otherwise) are not handed over while the marker is there: run such commands as the
controller's user, `docker compose exec -u 10002 controller ...` (or the data owner's uid, which readiness reports),
or delete `data/.auto-browser-owner` to force a new pass. **After running an older release on this data** (a
rollback to 1.13, which runs as root and does not know the marker), delete `data/.auto-browser-owner` before
starting 1.14 again, or what the older release created as root is not handed over. If an entry cannot be changed (a read-only or root-squashed
mount), the log has an ERROR naming the path; the controller still starts as that user, and whatever it then cannot
read fails with its own message.

Downloads: the controller never reads Chromium's own download files. Playwright's `save_as` streams each download
from the browser into the controller's artifacts, which the controller owns, so it works whatever uid either side
runs as (measured: shared and `docker_ephemeral`, the controller as 10002 and as a host-user uid; Chromium's own
files are 0644 in every case). CI's compose-smoke downloads a file and reads it back through the API; the check
itself reads the artifact as root, so it is the ownership check before it that proves the controller runs as the
runner's uid.

With the isolation overlay, the controller joins the group of the mounted Docker socket. On a Linux host that is the
`docker` group; **on Docker Desktop the socket is `root:root`, so the controller joins group 0**. It is still not uid 0
and holds no capabilities. Nothing in the image is writable through group 0 alone: the image takes the group write bit
off everything but the sticky, world-writable `/tmp`, `/var/tmp` and `/run/lock`, and `/home/controller` (sticky and
world-writable too, since any run-as uid needs a HOME). It also strips every setuid and setgid bit (su, passwd,
mount and the rest), so nothing a controller uid can run raises it. Access to the Docker socket is itself equivalent
to root on the Docker host, whatever user holds it.

A container that cannot switch users (no CAP_SETUID or CAP_SETGID: `cap_drop: [ALL]`, `cap_drop: [SETUID]`, or a
`cap_add` set without them) still starts, as a uid 0 with no capabilities at all and no_new_privs, whatever it was
given: the entrypoint empties every set but the bounding set, which only CAP_SETPCAP can clear, before the exec.
The log and readiness warn. That uid 0 still owns the image's files, though: a compromised controller could rewrite
`/entrypoint.sh` or `app/`, and a restart (`restart: unless-stopped`) runs that as root with the container's
capabilities again, which the WARNING names. The stronger forms are `cap_drop: [ALL]`, `user: "10002:10002"`, or
`read_only: true` with a tmpfs for `/tmp`. Under `read_only`, the tmpfs for `/tmp` is what OCR needs: without it,
screenshots are returned unscrubbed (flagged in the result, with a WARNING) unless `PII_SCRUB_SCREENSHOT_STRICT=true`,
which fails the call instead. The capability-less start works only on data root can read with no capability: on a
`/data` another uid owns (one an earlier start handed to 10002, say) it cannot read the 0600 API token, and the
entrypoint then refuses to start with one clear line (exit 70) rather than let the controller fail on it. It asks the
controller which file it would read (`python -m app.api_token --file-to-read`: an environment token or tokenless
loopback reads none; `API_TOKEN_FILE` from the environment or `.env` otherwise), whatever `/data`'s owner, and checks
that one. A token directory it cannot see, not there yet or under one it cannot search, is left to the controller,
which creates it or says why it cannot. A container started as a non-root
user runs the controller with no_new_privs too. To run such a container as the controller
user, start it as that user (`user: "10002:10002"` in compose, or `docker run --user 10002:10002`, with `./data` owned
by 10002): the entrypoint then runs the controller directly (measured: readyz, a session and the token all work with
no capability). Or add `cap_add: [CHOWN, DAC_READ_SEARCH, SETUID, SETGID]`, measured as the smallest set that hands
both a 1.13.0 data dir and a pre-release's 10001-owned 0700 directories over. Without DAC_READ_SEARCH (the set has
no DAC_OVERRIDE), root cannot enter another uid's 0700 directories: their entries are errors, and the pass (with no
marker) repeats on every start until they are handed over by hand.

**Data owned by 65534** (nobody: root-squashed NFS shows root's files that way): the controller runs as 65534, the
only uid that can read them there (root's chown cannot work on such a mount), and the log warns.

**A host user with uid 10001** (browser-node's): the controller never runs as 10001, so a `./data` owned by such a
user is treated like a root-owned one and handed to uid 10002. To keep the files yours, chown `./data` to another uid,
or start the controller as `user: "10002:10002"` and give yourself access through a group.

**A host shared with other users:** browser-node's endpoint file, `./data/browser-profile/browser-ws-endpoint.txt`,
is readable by any host user who can enter `./data`, and its `ws://` URL is what connecting to the shared browser
takes. It cannot be private to one uid: browser-node (uid 10001) writes it and the controller, another uid, reads it.
On such a host, keep `./data` closed to other users: owned by the user the controller runs as (above), with mode
0700, or 0750 with a group only the operators are in. The host scripts that create files there
(`scripts/bootstrap_cli_auth.sh`, `scripts/smoke_reverse_ssh.sh`) need write access as well: run them as the owner,
or make it 0770 for that group. browser-node, the per-session browser containers and the reverse-ssh sidecar are
unaffected: they mount the subdirectories they use directly.

`CONTROLLER_RUN_AS=root` restores the root controller of earlier releases. It is not recommended, and the
`controller_user` readiness check warns while it is set; that check also reports the uid in use and why.

## Per-session networks (docker_ephemeral)

With `SESSION_ISOLATION_MODE=docker_ephemeral`, each session's browser container runs on
its own Docker bridge network, `<ISOLATED_BROWSER_CONTAINER_PREFIX>-<session id>-net`
(`ISOLATED_BROWSER_NETWORK_MODE=per_session`, the default). The controller joins that
network before the container starts and leaves it when the session closes; the network
is then removed. A session's browser can reach the internet and the controller, but not
another session's browser by name, nor its Playwright server (9223), nor the shared
`browser-node` or any other service on the compose network. Published noVNC ports are
the exception (below).

The controller joins each session network at gateway priority -1, so its own compose network keeps its default
gateway and its published API port. That needs Docker Engine 28 or later (API 1.48). On an older engine Docker picks
the gateway by network name, lowest first, and weighs a network's other properties too, so a session network can
take it (and the published port with it) and the request creating the session can be cut. On such an engine the
controller logs one warning, starting "GwPriority is not honoured", at the first isolated provision, whatever the
names; when the session network's name sorts before the compose network's, it also names that order problem. Upgrade
the engine; setting `ISOLATED_BROWSER_CONTAINER_PREFIX` to a prefix that sorts after the compose network's name (the
default `browser-session` sorts after `auto-browser_default`) avoids only the name-order case.

- **Upgrading:** isolated browsers no longer reach compose-network services. A proxy
  sidecar named in `DEFAULT_PROXY_SERVER` or a proxy persona must be reachable some
  other way than by its compose service name, or keep `ISOLATED_BROWSER_NETWORK_MODE=shared`.
- **`ISOLATED_BROWSER_NETWORK`** now applies only to `ISOLATED_BROWSER_NETWORK_MODE=shared`,
  which puts every browser on one network as before. In `per_session` mode it is ignored,
  and the controller logs a warning at startup. `shared` has no session-network fence: a
  session's browser can reach the controller's API, which the bearer token and the Host
  allowlist still guard.
- **The controller must be a container on the isolation daemon.** A controller run on the
  host, or an `ISOLATED_DOCKER_HOST` that is a different daemon, cannot join session
  networks, so `per_session` refuses to start a session there and names
  `ISOLATED_BROWSER_NETWORK_MODE=shared`. A docker-socket-proxy in front of the daemon
  needs `NETWORKS=1` besides `CONTAINERS=1` and `POST=1`.
- **Address pools.** Docker's default address pools allow about 30 bridge networks per
  daemon, and the default bridge, the compose network and other projects take some of
  them. Above roughly 25 concurrent sessions (`MAX_SESSIONS`), widen the pools in the
  daemon's `daemon.json`, for example
  `"default-address-pools": [{"base": "10.200.0.0/16", "size": 24}]`. When the pools
  run out, session creation fails with an error that names `default-address-pools`.
- **Leftover networks** (a controller that was killed, or
  `ISOLATED_BROWSER_KEEP_CONTAINERS=true`) carry the label `auto-browser.managed=true`
  and are removed at the next controller start, after the leftover containers. Remove
  them by hand only after `docker compose down`, so the controller no longer holds an
  endpoint on them.

What per-session networks do not change:

- **The controller is on every session's network, but refuses it.** It joins each session
  network to reach that browser, and answers 403 to any request whose direct peer is on a
  session network, before the Host check and the token check (forwarded-for headers are
  ignored). Browser containers never call the controller. On Docker Desktop a session
  container can still reach the API port published on `127.0.0.1` through
  `host.docker.internal`, where the controller sees the host's gateway address; there the
  Host allowlist and the API token are what refuse it, so keep the token (the default).
- **Each session's published noVNC stays reachable from other sessions,** whatever
  `ISOLATED_BROWSER_BIND_HOST` is. Docker lets a published port be reached at the
  container's address from other bridge networks, and on Docker Desktop
  `host.docker.internal` reaches ports published on `127.0.0.1` (both measured on Docker
  Desktop 29.9). noVNC's WebSocket origin guard refuses a page's connection there, but
  native code in another session's container can forge the `Host` header. So each
  isolated session's noVNC has **its own random VNC password**, generated when the
  container starts; the operator's `VNC_PASSWORD` is no longer handed to isolated
  containers, where any of them could read it.
- **No agent-facing result carries the password.** No MCP tool result carries it, and no response
  of a route the agent-facing packages (the Python client, the MCP package, the LangChain
  integration) call: `create_session`, `get_session`, `get_remote_access`,
  `request_human_takeover`, observations, action results and `list_sessions` give the
  `takeover_url` without it, and an isolated session's `remote_access` block and takeover
  result add a `takeover_note` saying the operator opens the full link from the dashboard. The person taking over opens
  it from the dashboard ("Open noVNC") or prints it with `scripts/takeover_link.sh
  <session_id>`; both read `GET /sessions/{session_id}/takeover-link` (`Cache-Control:
  no-store`, audited as `takeover_link_viewed` without the URL), which no MCP tool and no
  agent-facing package calls. Besides the bearer token, which agents hold, that endpoint
  demands the takeover token in the `X-Takeover-Token` header, in every scope (tokenless
  loopback included), and agents are not given it: `TAKEOVER_TOKEN`, or the token the
  controller generates in `TAKEOVER_TOKEN_FILE` (`./data/takeover-token` with the shipped
  compose) on first start. Without it the answer is 403 (audited as
  `takeover_link_refused`), before any 404, and 503 when the controller could not load or
  write one (its startup log says why). The dashboard asks for it on the first "Open
  noVNC" of an isolated session in a tab, before it opens the noVNC window, and again
  on the click after a 403 (a shared browser-node session's link needs no token);
  `scripts/takeover_link.sh` reads it from `TAKEOVER_TOKEN` or `data/takeover-token`. The
  takeover token stops agents without file access to the controller's host, and only those:
  under compose `./data` belongs to the host user, so an agent running as that user on the
  host (a local MCP setup) can read `data/takeover-token`, and so can the controller's own
  `cli` providers wherever their sandbox is not in use (see "The CLI sandbox" under the CLI
  providers); so can anything that reads the session container's own copy of the password.
  Open the dashboard and the takeover link in a browser no agent drives: such an agent can
  read the dashboard's sessionStorage, where the tokens are kept. That link carries
  the password in its fragment, `#password=…`, so noVNC connects without asking; a
  browser never sends the fragment to a server, so it stays out of access logs, but
  treat the link like the API token. Opened without it, noVNC asks for the password.
  noVNC remembers a password it was given in that origin's browser storage, which is
  harmless: each session has its own random port and password.
- **The shared browser-node's noVNC is reachable from session containers too,** the same
  way. Only the operator's `VNC_PASSWORD` guards it, so set it with `docker_ephemeral`;
  the readiness check warns while it is unset.
- **On Docker Desktop, browser containers reach the host's loopback.** `host.docker.internal`
  reaches listeners bound to the host's `127.0.0.1`, not only the ports Docker publishes
  there. Measured on Docker Desktop 29.9 from the shared browser-node and from a
  `docker_ephemeral` session container on its own network: a page server on `127.0.0.1`
  answered, and so did a host Chromium's remote-debugging port once the request said
  `Host: localhost`. Its answer names the browser's DevTools WebSocket, through which CDP
  drives the whole browser and whatever it is signed in to (the WebSocket was not opened).
  The port refused the `Host: host.docker.internal` such a request normally carries, but
  native code in a browser container that a page compromised can send any header. So on
  Docker Desktop, don't leave a Chrome with `--remote-debugging-port` (what `CDP_CONNECT_URL`
  and `browser.cdp_attach` attach to), or any service that trusts loopback callers,
  listening on the host's loopback while browser containers load pages you don't trust. On
  Linux neither the compose files nor the controller give browser containers
  `host.docker.internal` (not measured on Linux).
- **Separation relies on Docker's firewall rules** between bridges. A daemon run with
  `"iptables": false` routes between them, and the networks isolate nothing.
- **One controller per daemon.** The startup sweep removes every labelled session network
  on the daemon, as the container sweep removes every labelled container.

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

A CLI child gets only the base names below, its own provider's variables (such as `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`) and the names in `CLI_PROVIDER_ENV_PASSTHROUGH` (comma-separated), never the
controller's tokens or other keys. The base names (`CLI_BASE_ENV_NAMES` in `controller/app/providers/base.py`), each
passed only when set, are `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LANGUAGE`, every `LC_*`, `TZ`,
`TERM`, `NO_COLOR`, `CI`, `TEMP`, `TMP`, `TMPDIR`, the proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`,
`NO_PROXY`), the CA variables (`SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`) and the
Windows names (`USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `APPDATA`, `LOCALAPPDATA`, `SYSTEMROOT`, `SYSTEMDRIVE`,
`WINDIR`, `COMSPEC`, `PATHEXT`). The other children the controller starts get the same base names: the Universal
Verifier command (plus the names in `HARNESS_UV_ENV_PASSTHROUGH`), autossh (plus its own `AUTOSSH_*` tuning),
Playwright's node driver and every browser it launches (plus Playwright's own `PW_*` and `PLAYWRIGHT_*`, `DEBUG`,
`DEBUG_FILE`, and `DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY`, `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` for
shadow browse's headed browser), and Tesseract, which screenshot redaction's OCR starts through pytesseract (plus
`TESSDATA_PREFIX` and every `OMP_*`); the regex worker gets only `SYSTEMROOT`, on Windows. On Linux the controller
also makes itself non-dumpable at startup, so a process of its uid cannot read the controller's own environment
through `/proc/PID/environ`.

#### The CLI sandbox

A provider CLI (`OPENAI_AUTH_MODE=cli` and the others) runs as the controller's user, and page content it is shown
can prompt-inject it. On Linux with Landlock (kernel 5.13 or later, with Landlock enabled) the controller starts each
provider CLI through a launcher (`controller/app/cli_sandbox_launcher.py`) that applies a Landlock ruleset and then
execs the CLI. Of `/data` the CLI can then open only `CLI_HOME`, to read and write, and the screenshot its own call
names (Codex's `--image`), to read. `data/api-token`, `data/takeover-token`, the auth profiles, the witness and mesh
keys, the tunnel key, another session's screenshots and everything else under `/data` answer `EACCES`, to a CLI that
runs as root as to any other, and so does listing `/data`. Outside `/data` a CLI reaches what it did before, with two
differences: it cannot list `/` itself or make an entry directly in it, and it runs with `no_new_privs`, so a setuid
program it starts gains nothing. Whatever a CLI needs under `/data` (its sign-in state, a `CODEX_HOME`,
`CLAUDE_CONFIG_DIR` or `GEMINI_CLI_HOME`, a CA bundle, the CLI's own binary) has to be inside `CLI_HOME`. On a kernel
older than 5.19 Landlock also refuses a sandboxed process every rename or link from one directory to another
(`EXDEV`), in `CLI_HOME` as anywhere.

Where the sandbox would leave a secret open, the controller writes one warning per process, which names the
condition or the settings and never a setting's value, and runs the CLI:

- **No Landlock.** The host is not Linux, its kernel is older than 5.13 or has Landlock switched off, the container's
  seccomp profile refuses Landlock's system calls (Docker's default profile allows them), or the launcher's probe
  failed for another reason: at the first CLI call the controller runs the launcher once in a mode that applies the
  ruleset and exits. The CLI starts without the launcher, as it did before 1.18.0.
- **`CLI_HOME` is `/data` or a directory that holds it.** The CLI starts without the launcher.
- **A secret is kept outside `/data`, or inside `CLI_HOME`.** `API_TOKEN_FILE`, `TAKEOVER_TOKEN_FILE`, `AUTH_ROOT`,
  `WITNESS_ROOT`, `MESH_IDENTITY_DIR`, `ISOLATED_TUNNEL_KEY_PATH`, `PROXY_PERSONA_FILE` and
  `BROWSER_WS_ENDPOINT_FILE` are checked, each resolved first, so a `/data` that is a symbolic link to another place
  holds none of them. The CLI starts under the launcher, which closes `/data` and not that place. A Linux host run
  that keeps its data elsewhere gets this warning, with the name of each of them it sets.

`CLI_SANDBOX_REQUIRED=true` refuses instead: with any of the three no provider CLI is started, the step fails with
`Provider request failed` (`502`), and the log names the condition in one error line. `GET /agent/providers` still
reports such a provider as configured. A launcher that cannot apply its ruleset at a real start, after its probe
passed, fails that call the same way, and the CLI is never started without it.

What the sandbox does not stop:

- **Metadata.** Landlock has no right for `stat`, `chmod`, `chown`, `utime` or extended attributes: to a file under
  `/data` whose path it knows, a CLI can do whatever of those the file's own permissions allow. It cannot list a
  directory there to learn a name.
- **Another CLI call's files.** Every call's CLI reads and writes all of `CLI_HOME`: the sign-in state, and the
  per-call directory of a call that runs at the same time.
- **Signals and connections.** The ruleset handles files and nothing of the network. A CLI is a process of the
  controller's user with the container's network: it can signal the controller, and it can open a TCP connection to
  the controller's port (a local client like any other, which lacks the API token wherever the controller demands
  one) and to whatever else the container reaches.
- **What the controller's user can open outside `/data`.** With the isolation overlay that includes the Docker
  socket.
- **Truncation, on a kernel older than 6.2.** Landlock could not deny `truncate` before its third ABI. There a CLI
  can empty a file under `/data` that the controller's user may write, though it can neither read nor write it.
- **A second mount.** The ruleset closes what is mounted at `/data` by leaving it out. If the data directory, or a
  part of it, is mounted at a second path of the container too, that path is allowed like any other beside `/data`,
  and what is mounted there is open to the CLI. Mount it once, as the compose files do.
- **The Codex host bridge's Codex.** It runs on the host as the bridge's user and not under this launcher, so it can
  read what that user can: `data/api-token`, `data/takeover-token` and the mesh and witness keys when the checkout
  is that user's. Use `api` mode where that matters.

The Playwright driver and the browsers it launches, like the provider CLIs, are ordinary
processes of the controller's user: their environment holds none of the controller's own tokens or keys (a proxy
setting that carries credentials excepted), but their memory holds page content and what is typed into pages, which
a command one of them runs could read where the host lets a process ptrace another of the same user
(`kernel.yama.ptrace_scope` 0). A provider CLI's key is readable by the commands it runs and by those another CLI
runs at the same time (`/proc/PID/environ` of a dumpable process of the same user). A Docker credential helper
configured for the controller's user (`credsStore` or `credHelpers` in its Docker config) is run by the Docker SDK
with the controller's whole environment when the controller pulls a session image the daemon does not have: build or
pull that image ahead (compose builds it) and configure no credential helper for that user.

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
The bridge starts Codex with an allowlisted environment, not the whole environment of the shell it was started
from. Exactly these pass, matched case-insensitively (the bridge's copies of the controller's `CLI_BASE_ENV_NAMES`,
`CLI_BASE_ENV_PREFIXES` and the OpenAI adapter's `cli_env_names`, which a test keeps equal):

- `CODEX_BASE_ENV_NAMES`: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LANGUAGE`, `TZ`, `TERM`,
  `NO_COLOR`, `CI`, the temp directories `TMPDIR`, `TEMP` and `TMP`, the proxy settings `HTTP_PROXY`,
  `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY`, the CA settings `SSL_CERT_FILE`, `SSL_CERT_DIR`,
  `REQUESTS_CA_BUNDLE` and `NODE_EXTRA_CA_CERTS`, and the Windows names `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`,
  `APPDATA`, `LOCALAPPDATA`, `SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `COMSPEC` and `PATHEXT`;
- `CODEX_BASE_ENV_PREFIXES`: any name starting `LC_`;
- `CODEX_PROVIDER_ENV_NAMES`: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `CODEX_HOME`, `CODEX_API_KEY` and
  `CODEX_CA_CERTIFICATE` (the PEM bundle Codex reads before `SSL_CERT_FILE`).

A Codex setup that needs another variable passes it with `--pass-env NAME` (repeatable; a name must be letters,
digits and underscores, not starting with a digit), for example `DBUS_SESSION_BUS_ADDRESS` or `XDG_RUNTIME_DIR`
for a sign-in kept in the desktop keyring where the bus is not at its default path, or `VOLTA_HOME`,
`ASDF_DATA_DIR` or `MISE_DATA_DIR` for a node version manager with a non-default home. That passes the
variable's value to Codex and to every command Codex runs, so pass only what Codex needs.

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

# The rest need the bearer token and, with REQUIRE_OPERATOR_ID=true, an operator id
# (/metrics takes the token only). The token is API_BEARER_TOKEN, else data/api-token
# (run from the auto-browser folder). For the production setup above, export it from
# .env first:
#   set -a; . ./.env; set +a
AUTH=(-H "Authorization: Bearer ${API_BEARER_TOKEN:-$(cat data/api-token)}" -H "X-Operator-Id: ops")
curl -fsS "${AUTH[@]}" http://127.0.0.1:8000/readiness | jq
curl -fsS "${AUTH[@]}" http://127.0.0.1:8000/metrics | head
curl -fsS "${AUTH[@]}" http://127.0.0.1:8000/maintenance/status | jq
```

`/readiness` answers `503` when a check fails, so `-f` prints nothing; drop `-f` to see which check.
If `METRICS_ENABLED=false`, skip the `/metrics` check; the endpoint returns `404`.

## Stopping the controller

`docker compose stop` and `restart` send the controller SIGTERM. uvicorn stops taking connections and waits up to 5
seconds for the requests in flight (the image starts it with `--timeout-graceful-shutdown 5`), cancels what is still
running, and then the controller cleans up: the maintenance loop, the cron scheduler and the job queue stop, and
every session is closed, one after another (an isolated session can take about 10 seconds). Compose gives the
controller `stop_grace_period: 60s` for all of it, so the cleanup has 55; Docker kills a controller still running
after that, and a session whose close was cut short can leave its container or tunnel behind.

An event stream a client holds open (`GET /mcp`, `GET /sessions/{id}/events`) never ends by itself, so a stop with
one open always waits the 5 seconds. The log then shows uvicorn's
`Cancel N running task(s), timeout graceful shutdown exceeded` at ERROR, and one INFO line from the controller for
each stream it ended (`the event stream on '/mcp' was still open at the shutdown limit: ended`). That is a normal
stop, not a crash: the stream's body is finished and its connection closed, and a client can open it again when
the controller is back. A client that is still connected and has stopped reading cannot be sent the end of the body:
the controller waits up to one second for it and logs that the stream was `left unfinished`, and uvicorn logs
`ASGI callable returned without completing response.` at ERROR and asks the transport to close. A connection that
still holds bytes its client has not taken stays until they are gone or the process ends. A call still running at
the limit is cancelled too, and for that one uvicorn logs `Exception in ASGI application` with a traceback that ends
in `CancelledError`.

If you start uvicorn yourself (your own image or command, a process manager), give it the limit too, in seconds and
well under what your supervisor waits before it kills the process:
`uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 5`. Without it uvicorn waits for an
open stream without limit and the cleanup never runs.

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
`--disable-features=BlockOriginHeaderModificationOnRedirect,AvoidCorsURLLoaderRestartOnRedirect` and set
`CDP_BLOCK_ORIGIN_ON_REDIRECT_DISABLED=true`.
- Without the first feature, older Chrome fails a cross-origin 307/308 POST redirect hop that a DevTools `Fetch`
  handler lets through (Chromium 151 did, in the operator's own tabs too). So the controller stops such a hop in
  its sessions itself, with an error naming the switch.
- Without the second, Chrome 155 and later abort a credentialed cross-origin request that needed a CORS preflight
  once it is redirected, in every tab, while the gate is attached (Playwright #43242; Playwright disables the
  feature in the browsers it launches, so `browser-node` is not affected). The controller logs a warning,
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

A window is also judged when the browser first reports its target to the controller, before any of its
navigations — through CDP (the window-birth watcher, one browser-level session per browser), from the
target's opener, not from a request. Playwright reports a window only once it commits, which may be after
the agent's navigate has cleared the opener, so a window that committed nothing until then (a `204` or
`205`, a failed or hung request, no request at all: `javascript:` or a blocked top-level `data:` URL, or a
blank the opener writes into) would otherwise be judged by the clean opener and let through. The verdict at
birth is keyed on the opener, not the session:

- the opener is a window the watcher already judged born refused, reported or not: born refused, whatever
  the session does meanwhile;
- the opener is a page the controller knows (the session's first page, a tab the open-tab tool opened, a
  reported popup; each has its target id cached): born refused when that page is refused at that moment, or
  when a tab that counted for the session closed within the grace below;
- the opener cannot be matched to a page but the watcher saw it born clean (a popup reported a moment ago
  whose target id is still being read, or a clean window Playwright has not reported, which sits on its own
  clean opener's blank): born clean when no open page of the session whose target id is unknown is refused,
  whichever of them the opener is; otherwise the session decides, as below;
- the opener cannot be matched at all (the watcher has no entry for it): born refused when the session
  counts as refused, and that refusal names another tab as its cause, as a `by another tab` refusal does;
- no opener (the controller's own new page: a session's first page, an open-tab tool tab): never.

A page's own verdict does not count against the windows it opens while that verdict is being judged: a popup
held until its birth verdict lands is judged, for them, by what happened to it (its URL, a taint, a refused
hop), and if it was born refused its windows are born refused by the first rule. A page held with no judgement
running (its opener closed before Playwright reported it, or its judgement ended without a verdict), or held
for a birth the watcher never saw, refuses the windows it opens, as before the watcher.

A born-refused window Playwright reports is refused as a popup of a refused opener is. A reported window whose
opener is clean at report is held refused until its birth verdict is known (one CDP read of its target id,
read up to three times in all when it fails): what it logs and throws meanwhile is kept aside (at most 50
entries) and reaches the agent once the verdict is clean, also when the popup closed before the verdict landed
(dropped on a refusal, and for a popup that closed before its id could be read, which leaves it unjudged), and
`activate_tab` waits for the verdict (before it takes the session, so the session's other calls do not wait
with it; at most 10 seconds) rather than refuse the window. Two bounds, then: `activate_tab` waits at most 10
seconds for a held popup's verdict, and a navigate or a rollback whose sweep (below) has work waits at most 3
seconds, under the session lock, for the target id reads in flight of the session's reported pages (not for
one that has failed already and is being retried: the session is swept again as soon as that retry ends, and
an id the sweep's own later re-read finds is used by the next sweep, within a second). A born-refused window
Playwright never reports is closed through CDP as soon as nothing justifies it: its opener is clean (the
agent's navigate, a blocked action's rollback) or gone, past the grace; or, for a window refused by the
session fallback or one whose birth was not judged (below), the session no longer counts. That sweep runs
right after the agent's navigate and a rollback's `go_back`, after a counting tab's close, and once a second
while any such window is live, so it does not outlive the refusal whatever ended it. It never closes a page
Playwright has reported: a reported page's target id is cached once read, and the sweep closes only a window
whose id is none of the session's pages'; while a reported page's id is not yet known (its read is in flight,
or failed and is being read again) it closes nothing it cannot match in that session, since any of it could be
that page; and each window is checked again right before its close, for a report that landed during the wait.
An open-tab tool tab has no opener, so no born-refused window can be it: its id, read or not, does not hold
the sweep (a session's first page always has its id read when the session is created, or the session is not
created). A reported popup whose id could not be read at all is refused as unjudged (below) and, while it
stays open, spares that session's unmatched windows, which can act meanwhile; the sweep reads its id again
every 5 seconds while it has work, so a read that failed in passing does not spare them for the popup's life,
and if the id can never be read, close the popup. Only page targets are judged this way; iframes and workers
are not windows, and a window a cross-site iframe opens has the iframe's page as its opener, so it is judged
by that page (an iframe from another host on an allowed page can open a window, judged by the allowed page, as
the iframe's own content is not filtered).

The watcher runs whenever `ALLOWED_HOSTS` refuses a host, with `PRE_REQUEST_BLOCKING` off and in attach modes,
and fails closed. A browser it cannot attach to, or a context whose id it cannot read, gets no session:
creating one answers `window_birth_watcher_unavailable` (503, retryable, as the gate's
`pre_request_gate_unavailable`), and the next session tries again after a 2-second backoff. A reported window
whose target id cannot be read, or whose birth the watcher never saw, is refused and sent to `about:blank` as
a refused popup is, but as *unjudged*: nothing is known against its host, and the refusal says so and names
the way out (activate: "That tab could not be judged when it opened ... Close it and open its URL in a new
tab: a tab opened once /readiness reports the window_birth_watcher attached is judged as it opens."; reads:
"The page could not be judged when it opened ..."). Activating that tab again does not succeed: a verdict,
once reached, is final. (A judgement still running when `activate_tab`'s 10 seconds pass could still land
clean later; nothing known runs that long, since a target id read that hangs gives up after 7.5 seconds: three
reads of at most 2 seconds, and the waits between them.) It is blanked rather than kept open because,
unjudged, it may be a refused page's window with a script planted in it. If the watcher's session dies while
the browser lives and has a watched session, a replacement attaches at once; until it is up, every popup
reported in those sessions is refused as unjudged, and stays so until the agent navigates it (a replacement
cannot judge a window born before it). An attach that fails is tried three more times, about 2 seconds apart,
while the browser still has a watched session; after the last one, and where the watcher was lost with no
session watched, the watcher stays lost until a session is created on that browser.
The replacement registers every live session on that browser before it discovers the browser's
targets, so the windows that exist by then are its entries too: a page the controller knows (its target id
cached) counts as reported and clean; a window with an opener is unjudged, refused if it is reported and swept
as soon as the replacement is up and the session does not count; a window with no opener is clean. While no
watcher is attached nothing closes such a window, so a window a refused page opened during the loss can act
until the replacement sweeps it: a navigation it makes to an allowed URL then is reported, refused as unjudged
and blanked, and reaches neither the agent nor the console, but that URL, with whatever the refused page put
in it, has reached the allowed origin's server (the same class as a refused page's own request, above).
Reading a page's target id opens a page-level CDP session that is kept for the page's life, one per reported
popup or open-tab tool tab, since detaching it lost the failed-request and download events of a window that
page opened (under the gate too). With `ALLOWED_HOSTS` empty or `*` nothing attaches: no browser-level CDP
session is opened, no target id is read for this, and a popup is judged at report by its opener alone, as
before the watcher. `/readiness` reports a `window_birth_watcher` check next to the gate's (off, idle,
attached, lost or attach failed).

### Reading a refused page again

Observe, screenshots, the page-reading MCP tools, the network log (`GET /sessions/{id}/network/requests`,
`GET /sessions/{id}/network-log`, `browser.get_network_log` and the `browser://<id>/network` resource) and
actions other than navigation answer that a refused page is not read (REST: 403), also after the controller
has sent it back to `about:blank`, and after the page has loaded an allowed URL on its own (a refused host's
redirect, a script). The taint clears only in two ways:

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
error page, a fork without `start_url` starts on a blank page with the source's cookies, as from
`about:blank` (a refused error page still answers the 403), and a shadow browse answers 400 with a fixed text:
the page is Chrome's error page for a navigation that was stopped or failed.

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

Console messages and page errors are not kept while the list refuses their page. A reported popup held only
for its birth verdict has its console messages and page errors kept aside (at most 50) and appended if the
verdict is clean, including when the popup closed before the verdict landed; a refused verdict, or anything
else refusing the popup meanwhile, drops them, and so does a popup that closed before its target id could be
read (unjudged).

The network log is not served at all while the session's active page is refused: `GET /sessions/{id}/network/requests`
and `GET /sessions/{id}/network-log` answer `403`, `browser.get_network_log` is a refusal and
`browser://<id>/network` answers `-32603`. Navigate the page to an allowed URL to read it again (see above); what it
holds then is below. A page that moves onto a refused host while the log is read is refused too, and a session
another call holds past the bound answers `409` over REST.

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
`browser.get_html`, `browser.find_elements`, the storage tools and `browser.set_viewport` (refused before
a write and checked after it), `browser.drag_drop`, the DOM resource and the CDP routes. Tab listings, session summaries, approval
requests and saved auth profiles leave the title empty in the same cases, and a bot challenge found on a
page that moved while it was probed is refused instead of described.

A refused page's URL is shown by its origin only (`scheme://host:port`) in tab listings, session
summaries, the snapshots an action returns, approval requests and the `navigation_blocked` audit event.
`about:blank` is shown whole, and so is the URL of a page that is allowed again by the time it is shown.

The page-reading MCP tools (`browser.eval_js`, `browser.get_html`, `browser.find_elements`,
`browser.get_local_storage` and `browser.set_local_storage`, for local or session storage,
`browser.drag_drop` and the `browser://<session>/dom` resource) and `browser.set_viewport` take the
session lock: they wait up to
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
seconds. `POST /sessions/{id}/cdp/raw` answers 422 to a `method` that is not a string or `params` that
are not an object (left out or `null` is none), and each allowed command takes only its own parameters,
by name and type (`_ALLOWED_CDP_PARAMS` in `app/cdp/passthrough.py`): any other answers 422 and says why.

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
  it refused). The exception is a reported popup held for its birth verdict: its console messages and
  page errors are kept aside (at most 50) and appended if the verdict is clean, including when it closed
  before the verdict landed, but not when it closed before its target id could be read (unjudged).
- **Tabs.** A tab still waiting for the page that opened it to judge it (that page closed first) stays
  refused until the agent navigates it or it is closed, and the windows it opens are refused. On an
  allowed http(s) URL it does not refuse the other tabs; on a blank, `blob:` or `data:` page, which can
  hold what that page wrote, it does. An iframe from another host on an allowed page can open a popup,
  which is judged by its opener, the allowed page. A popup is judged by its opener as the opener is when
  Playwright reports the popup, and by its opener as it was at the popup's birth (see [When a page is
  refused](#when-a-page-is-refused)): a window a refused page opened just before the agent's navigate made
  that page readable again is refused by the birth verdict, whatever its first navigation did, and so is
  any window it opens in turn. A reported popup whose opener is clean at report is held refused until its
  birth verdict is known, a CDP round trip (`activate_tab` waits for it, at most 10 seconds, rather than
  refuse the popup, and the session's other calls are not held behind that wait), so it is readable a
  moment later than before, and what it logged meanwhile is appended once the verdict is clean; a window
  the held popup opens meanwhile is judged by the popup's own birth, not held against it. A popup whose
  target id the controller could not read, or that was reported while the window-birth watcher was down,
  is refused as unjudged and sent to `about:blank`: the refusal says the tab could not be judged, and
  nothing is known against its host; close it and open its URL in a new tab, or, if it is the active tab,
  navigate it (activating it again does not succeed). A born-refused window Playwright never reports is closed
  once its opener is clean or gone (or, when its opener could not be matched or its birth was not judged, once
  the session stops counting), so it cannot keep an opener reference across a rollback or send itself to an
  allowed URL later; a navigate or rollback whose sweep has work waits up to 3 seconds for the session's
  target id reads in flight. The exception is a reported popup whose target id could not be read: while it is
  open, the sweep closes nothing it cannot match in that session (any of it could be that popup), so a
  born-refused window is spared, and can act, until the popup's id is read again (every 5 seconds) or the
  popup is closed. The grace cuts the other way too, by design: a legitimate popup (an OAuth window, a
  `target=_blank` link) that a clean tab opens within 2 seconds of a refused tab closing (the agent closing
  it, or the tab itself) is born refused, and so refused; close it and open it again after the grace, or wait
  2 seconds after a refused tab closes before starting a popup flow. The 2 seconds after a refused tab closes
  are measured by the clock: a commit that comes with no request (a `#fragment`, `about:blank`) and that a
  slow renderer delivers later than that is not refused for that tab.
- **Traces and exports.** Playwright traces (`ENABLE_TRACING`, off by default) record the whole browser
  context, the refused page included, and a storage-state export (`POST /sessions/{id}/storage-state`,
  saving an auth profile) holds every host's cookies.
- **Iframes and subresources** from other hosts on an allowed page are not filtered, by design.

## Witness receipts: what a signature proves

Each receipt is hash-chained to the one before it and signed with an Ed25519 key. The key is generated on first use in `<WITNESS_ROOT>/keys`, beside the receipts it signs. That decides what the signature can prove:

- **It proves the receipts are consistent and were signed by that key.** `GET /sessions/{id}/witness/verify` and the `browser.verify_witness` tool report this: the hash walk, the anchor, and a `signatures` block. A chain with no signed receipt fails the `signatures` check, and so does an unsigned receipt after a signed one. The top-level `valid` passes only when all three do. `chain_valid` is the hash walk and the anchor alone, and `reason` says which check failed. Missing evidence is never `valid`: a chain with no receipts (deleted, or never written), a chain whose anchor file is missing or unreadable (`anchor.status` is `anchor_missing` or `anchor_unreadable`), and a chain shorter than its anchor all fail. An id with no session record, no chain and no anchor answers `404`, for verify and for the bundle. Under named credentials (`API_BEARER_TOKENS`) an id with no session record answers `404` even with a chain, since no owner can be checked for it: the chain of the auth-profile export and import receipts (`auth-profiles`) is one, so its witness routes and tools answer `404` to every operator. Chains written before 1.7.0, when anchors arrived, are anchored as they are on the first start after upgrading (the root is then marked `.anchors-migrated` and no later start anchors anything), so their hash walk and anchor keep passing, though a chain written wholly before 1.6.0, when receipts were first signed, still fails the `signatures` check; anything lost from them earlier cannot be detected. That start anchors a chain of any release that lacks its anchor, so a chain whose anchor was deleted on 1.11.1 or earlier is anchored as it stands too: if its evidence matters, check the chains without a `<id>.jsonl.anchor.json` before the first 1.11.2 start. A later receipt never moves the anchor over a chain that was cut short or lost its anchor: the chain stays flagged, and the receipt is still recorded. With `WITNESS_ENABLED=false` no session receipts are recorded (auth-profile export and import still record theirs), so a session that has none verifies `false`; receipts recorded earlier still verify. Anyone who can write the witness directory can still make a cut chain verify: by rewriting the anchor; by deleting the chain together with its anchor, after which the next receipt starts a new chain that verifies; or by deleting `.anchors-migrated` before a restart, which anchors a cut chain whose anchor is missing as it stands. For evidence that must survive that, pin a head hash off the box (below). A crash or a full disk between a scope's first receipt and its anchor leaves that chain `anchor_missing` for good. A receipt whose anchor update failed leaves the chain one receipt ahead of its anchor. Until the next successful append moves the anchor over it, verify reports the chain `diverged` (`valid: false`, "the chain was rewritten or forked"), and that receipt can be removed without verify noticing. If no receipt follows (it was the scope's last), the chain stays `diverged`. To recover a chain you have checked by other means, delete `<WITNESS_ROOT>/.anchors-migrated` and restart: that anchors every chain that has no anchor file, as it stands, so it also anchors any cut chain whose anchor was deleted. A chain whose anchor file is still there (unreadable, truncated, or diverged, such as one whose last anchor update failed) is skipped: delete its `<id>.jsonl.anchor.json` as well.
- **It does not prove anything against someone who controls the box.** Whoever can write the witness directory can also read the key, so they can edit receipts and sign the result. A bundle that verifies tells a recipient the receipts match the key in the bundle, not that the key was never in the wrong hands.

How verify reads a chain, since 1.11.2: `valid` is `false` when the chain file is gone (even with its anchor left behind), when a chain has no receipts, when its anchor file is missing (`anchor_missing`), and when the anchor is corrupt, not UTF-8 or malformed (`anchor_unreadable`: the count must be an integer of at least 1 and the head a non-empty string). The chain's length and head must match the anchor exactly; `truncated` and `diverged` fail as well. The `anchor.status` value `unanchored`, which passed, is gone. A final receipt that parses but lost its newline counts as a receipt in verify, list, export and the next append, which completes it instead of setting it aside. A chain with an unparseable record fails the signature check with a verdict, not an error. `chain_valid_at_export` in a witness bundle follows the same rule, and `scripts/verify_witness_bundle.py` fails a bundle with no receipts. List and export skip a line that does not parse instead of failing, and report how many in `unparseable_lines` (in the listed window for `GET /sessions/{id}/witness`, across the whole chain in a bundle). A torn tail (bytes after the last newline that do not parse: an append cut short, which the next append sets aside) is skipped by both and not counted; a listing or a bundle that skipped one says `torn_tail: true` (the listing since 1.17.0), `scripts/verify_witness_bundle.py` adds a note when a bundle does, and the bundle's `chain_valid_at_export` is false until that next append, since verify still reads the fragment as a record that does not parse. A bundle with `unparseable_lines` above 0 has a `null` `head_hash`. `chain_reason_at_export` is the exporter's own check's reason, as a sentence, and `chain_anchor_status_at_export` its anchor status code (`ok`, `anchor_missing`, `anchor_unreadable`, `truncated` or `diverged`; `null` when the check failed before it reached the anchor). `scripts/verify_witness_bundle.py` fails a bundle whose `unparseable_lines` is present and anything but the integer 0, or whose `chain_valid_at_export` is present and anything but `true` (a bundle made before that field passes without it), and prints the bundle's own text with control characters escaped. These fields are the exporter's own report and are unsigned: they catch an honest incomplete export, not a forged one.

For evidence that survives a compromised controller host, keep a head hash from before the compromise and check it when you verify: `python scripts/verify_witness_bundle.py bundle.json --expect-head <head_hash> --expect-key-id <key_id>`. The `head_hash` and `signing_key_id` come from `GET /sessions/{id}/witness/bundle`; the verify result has the `head_hash` too. Sending a copy of each receipt to a hosted Witness service (`WITNESS_REMOTE_URL`), or copying the key id and head somewhere the controller host cannot write, works as well. Without such a pin, a recipient can only check that the bundle is internally consistent.

## What PII scrubbing covers, and what it does not

`PII_SCRUB_*` matches known secret and personal-data shapes (emails, phone numbers, card numbers, API tokens, private-key blocks and so on) in a few specific outputs. It is a safety net for those outputs, not a filter over everything the browser can see.

**Scrubbed**

- **Screenshots** (`observe`, `capture_screenshot` and the before/after action snapshots): matching words are painted over using OCR boxes. This needs the `tesseract` binary, which only the Docker image ships. Outside Docker, or with `OCR_ENABLED=false`, screenshots are returned **unscrubbed**: `/readiness` reports a `screenshot_scrubbing` warning, the controller logs one warning, and each result carries `screenshot_pii_scrub: {"applied": false, "reason": ...}`. A redaction that fails while it runs (an image Pillow cannot process) is reported the same way, with reason `redaction_failed`, and audited as `failed`, not `ok`. OCR is also best-effort, so small or low-contrast text can be missed.
  Set `PII_SCRUB_SCREENSHOT_STRICT=true` to withhold instead: when a screenshot cannot be scrubbed (OCR unavailable, or a redaction that fails) it is deleted and the call fails with code `screenshot_withheld` (HTTP 503), and with OCR unavailable `/readiness` reports `screenshot_scrubbing` as `fail` rather than `warn`. `PII_SCRUB_ACTION_SCREENSHOTS=false` still leaves the before/after action snapshots unredacted, by choice. An action whose after-screenshot is withheld has still run: it is reported and recorded, with `observation_error` in place of the image. An action whose before-screenshot is withheld (with `PII_SCRUB_ACTION_SCREENSHOTS` on, the default) is refused before it runs, with the same 503: nothing happened, and a retry is clean. So with OCR unavailable, strict mode refuses every action, `navigate` included, as `/readiness`'s `fail` says. The default is `false`.
- **Network inspector bodies and URLs**: request and response bodies stored by the network inspector. Text bodies are scrubbed whatever their content type says; binary content types and bytes that are not UTF-8 are stored as they are. URL-encoded form bodies are matched as they arrived and with their `%XX` escapes and `+` signs decoded, and only the stretch that matches is replaced, so the rest of the form is stored byte for byte. A parameter whose name marks a secret has its whole value redacted whatever it looks like, and keeps its name. Any segment of the name can mark it: a word of `password`, `passwd`, `passphrase`, `pwd`, `pass`, `secret` or `token` (`password_confirmation`, `user[password][new]`, `access_token`, `csrf_token`, `db_pass`, `wifiPass`, and also `token_type`), a trailing `key` (`api_key`, `apiKey`), or exactly `cvv`, `cvc`, `csc` or `pin`. A name that merely holds the letters (`tokens`, `tokenizer_name`, `secretary`, `spin`, `keyboard`, `passport`, `passenger_count`, `passed`) does not count. Neither do `public_key`, `publishable_key` and `site_key`, which are shown to every visitor, nor a paging cursor, which points at another page of a list: `page_token` with `next`, `prev` or `previous` in front of it (`next_page_token`, `nextPageToken`, `prev_page_token`, `previousPageToken`). `page_token`, `pageToken` and `page_token_value` are secret names, because a Facebook page access token is the value of one. (The name rule is all that spares a cursor: in a form or a query, 30 or more base64 characters right after `token=` are still redacted by the base64-secret pattern, name included, so a long `next_page_token` there is stored as `next_page_[REDACTED]`.) JSON and JavaScript bodies are matched as they arrived, with their `\uXXXX` escapes and `\/` written out (all but the quote, backslash, control-character and surrogate escapes, which must stay escaped to keep the JSON valid), and with only the escapes of letters, digits and `@._%+-` written out; every match is redacted in the text as it arrived. A JSON member whose name marks a secret by the same rule (`access_token`, `refresh_token`, `id_token`, `client_secret`, `api_key`) has the text of its string value redacted and keeps its name, so an OAuth token response stays valid JSON. A body with no escape is read the same way as one with escapes, and in JSON the password, `api_key`, Azure-secret, base64-secret and GCP service-account key-id patterns redact the value alone, so the quotes around it stay and the body still parses when the value is a string (a service-account key file keeps its `"private_key_id"` member). A card number is found beside other groups of digits too (`4111 1111 1111 1111 12/27`). The query string and the fragment of request URLs and of the `Referer` and `Location` headers are scrubbed the same way (an OAuth implicit grant or an OIDC response in fragment mode carries `access_token` and `id_token` in the fragment), and a query or a fragment longer than `NETWORK_INSPECTOR_BODY_MAX_BYTES` (counted in UTF-8 bytes) is replaced, each on its own; a query that is too long takes the fragment with it. A redaction in any of them sets the entry's `pii_redacted`. The URL of a failed request that is kept in the session's `request_failures` (returned by `get_request_failures`, and the last ten in every observation) goes through the same scrub, with the same size cap and under the same `PII_SCRUB_NETWORK` setting, whatever `NETWORK_INSPECTOR_ENABLED` and `NETWORK_INSPECTOR_CAPTURE_BODIES` say; with network scrubbing off it is stored as it is, except that a query or a fragment over the cap is still replaced, as in the inspector's URLs (under `PII_SCRUB_ENABLED=false` the inspector stores it whole). A body the scrubber cannot process, or whose result is not text, is withheld, not stored. When a scrub fails, the warning in the log names the URL as the inspector stores it (its query scrubbed) and the exception type for a body, and only the origin and the exception type for a URL; never the body. A PEM private-key header with no `END` line redacts everything to the end of the text, so in a form it takes the parameters after it too. Network hooks (`POST /sessions/{id}/network/hooks`) match the stored, redacted URL: a hook registered on a query value (`*email=ops%40example.com`) no longer fires, while one on the path or on a parameter name still does. Headers such as `Authorization` and `Cookie` are masked separately.
- **Console messages** returned by `get_console_messages`.
- **Downloads read with `read_download`**, under `PII_SCRUB_ENABLED`: the text is scrubbed by the MIME type its file name maps to in Python's built-in table (the controller does not read the host's), as a network body is by its content type, whatever the file holds. A name whose type is JSON or JavaScript is scrubbed by its members' names: on the image's Python 3.11 that is `.json`, `.js`, `.mjs` and `.webmanifest` (`application/manifest+json`). Any other, CSV and a name with no type included, is scrubbed as plain text; no name maps to a form type. The result's `pii_scrub` says whether the scrub ran and how many redactions it made, and a read that redacted something is audited as `pii_redaction` (layer `download`) under `PII_SCRUB_AUDIT_REPORT`. The file itself, served at the download's artifact URL, is not changed.

**Not scrubbed**

- **Page content, by design**: `get_html`, `find_elements`, the DOM outline and element text in an observation, cookies and storage, a downloaded file fetched from its artifact URL, and the output of `eval_js`.
- **Raw CDP**: `raw_cdp_command` returns what the browser returns, but for what a password field holds. `DOM.getDocument` with `{"depth": -1, "pierce": true}` returns each input's user-agent shadow text, which is what was typed into it; in a `DOM.*` read the passthrough blanks that text, and the `value` attribute, of any element whose `type` attribute is `password`, and `GET /sessions/{id}/cdp/element` blanks that `value` attribute in the attributes it returns. The accessibility tree masks password values itself (`•••`). A field of any other type keeps what was typed into it, a one-time code or a secret typed into a text field included, and a copy a page's own script makes of a password (into another field, an attribute or text) is page content like any other. Treat anything an agent types into a field that is not `type="password"` as readable by every caller allowed to use raw CDP.
- **The last console messages inside an observation** (`console_messages` in an `observe` result), and page errors. Use `get_console_messages` for the scrubbed view. (The URLs in request failures are scrubbed: see the network inspector entry above.)
- **The page's own URL** (`url` in every observation, and in the prompts of the built-in agent loop), which keeps, for example, an OAuth implicit grant's `#access_token=...`.
- **The audit log**: events are stored as their callers write them, so an event's details keep what the action carried (a navigated URL with its query, for example). `PII_SCRUB_AUDIT_REPORT` only adds `pii_redaction` events: one for a scrub that redacted something, with its layer, hit count and pattern names, never the values, and one marked `failed` for a screenshot redaction that failed.
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

`VNC_PASSWORD` adds VNC authentication to the shared browser-node's noVNC; noVNC prompts
for it. Isolated session containers get a random password of their own instead (see
"Per-session networks" above). Set it whenever 6080 is reachable
beyond loopback — but treat it as a second layer, not the gateway: the VNC protocol
uses only the first 8 characters and a DES-based challenge. With `docker_ephemeral` it is
also what keeps code in an isolated session's container out of the shared browser-node's
desktop, which those containers can reach.

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

## Cron jobs and webhook keys

Schedules are read in the zone `CRON_TIMEZONE` names, `UTC` by default, whatever the host's own zone is: set it to an
IANA zone name such as `America/Chicago` for a job to fire at that zone's hour. A name the zone database does not know
stops the controller at startup, and the name is in the message. The controller image carries the zone database. In a
zone with daylight saving time, a job set inside the hour the clocks skip fires an hour later by the clock that night,
and one set inside the hour they repeat is due twice, once in each pass of that hour (`30 1 * * *` in `America/Chicago`
is due at 01:30 before the change and again at 01:30 after it). The second is a fire like any other: it is skipped,
with a warning in the log, while the run the first started is still queued or running, and, as for every fire, one the
scheduler reaches more than five minutes late is not made and several it missed are made once.

A cron job runs on a schedule, or when `POST /crons/{job_id}/trigger` is sent its webhook key (on a controller with a
token, the bearer token is needed as well, as on any route). The key is 256 random bits, answered once by the call that
makes it, `POST /crons` or a rotation. The store keeps only its SHA-256 hash, and reads answer `webhook_key_id`, a short
non-secret id derived from the hash that tells keys apart:

- **Rotate** with `POST /crons/{job_id}/webhook-key/rotate` or `browser.rotate_cron_webhook_key`: a new key, answered
  once; the old key stops working at once, and a job with no webhook gets its first.
- **Revoke** with `DELETE /crons/{job_id}/webhook-key` or `browser.revoke_cron_webhook_key`: the key is removed, and
  a trigger is refused as for a job that never had one. A rotation brings the webhook back.

A trigger answers `403` for the key and for nothing else: an unknown job, a job with no key and a wrong key, with
nothing to say which. With the key proved, what stops the run is answered as itself and never as `403`: among them
`409` for a disabled job, for the session cap, for an auth profile that is gone or has become another operator's and,
in one fixed text that names no cause, for anything else the controller refuses of the run's session or of the job
as it is stored (an auth state older than `AUTH_STATE_MAX_AGE_HOURS`, a `start_url` that `ALLOWED_HOSTS` no longer
lists or whose load the pre-request gate stops, a stored field of a type a run cannot take); `422` for an auth state
that cannot be decrypted; `503` for a full job queue, for a browser that cannot be reached and, in one fixed text
that says the key was accepted and to try again, for a `start_url` that did not load or a browser that was not ready
for a session. The server log has the cause of that fixed `409` and of that `503`. A `400` is the request itself: its
body, or a job id in the path that is blank or longer than 50 characters.

Each change writes one audit event (`cron_webhook_key_rotated`, `cron_webhook_key_revoked`) without the key or its hash, and
another operator's job answers as one that does not exist. A key made before 1.18.0 is replaced by its hash on the
first start after the upgrade and goes on working; if the store cannot be re-saved then, the job keeps working from
the plaintext key still in the file, and the failure is logged and retried at the next start.

A job's `last_status` reads `queued` from the fire that queued a run until that run is over, and then the agent
job's final status (`completed`, `failed`, `cancelled`, `interrupted` or `discarded`), or `unknown` when its record
cannot say.
`completed` means the run ended without an error, not that it reached its goal: a run that stopped at its step limit,
or to wait for an approval or a takeover, reads `completed` too, and the agent job's `result.status` says which.

## Backups

Back up at least:

- `/data/db/` if using SQLite
- `/data/sessions/`
- `/data/jobs/`
- `/data/auth/` if you intentionally keep reusable auth state
- `/data/audit/`
- `/data/witness/`, whole: the chains with their `*.anchor.json` files, `.anchors-migrated` and `keys/` (since 1.11.2 a chain restored without its anchor fails verification, and receipts verify only against the key that signed them)
- `/data/crons/` if you use cron jobs
- `/data/approvals/` if pending approvals and operators' rejections should survive a restore, whole, with its `.tool-call-digest.key` (a glob such as `approvals/*` leaves a dot file out): approvals for tool calls are matched under that key, also when `/data/db/` holds the records, and restored without it they are refused and asked for again, and an operator's rejection of a tool call no longer stops an agent's own approval of the same call under `AUTONOMOUS_APPROVALS` (each start then logs a warning with how many pending or approved approvals are kept under the lost key, if there are any, and another with how many operator rejections are, if there are any); a copy taken between an approve and its claim brings that approval back as `approved` until it expires (`APPROVAL_TTL_MINUTES` after the approve), so after restoring one, reject any whose action already ran

## Cleanup

The controller can now prune stale artifacts/uploads/auth-state automatically.

Manual run:

```bash
curl -s http://127.0.0.1:8000/maintenance/cleanup \
  -X POST \
  -H "Authorization: Bearer ${API_BEARER_TOKEN:-$(cat data/api-token)}" \
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
