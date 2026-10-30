# Changelog

All notable changes to auto-browser are documented here.

## [1.12.0] — 2026-10-29

A controller feature release over 1.11.2: pre-request blocking. A top-level navigation to a host `ALLOWED_HOSTS`
refuses is now stopped before Chromium sends it, rather than refused after the page loads. The client, LangChain and
MCP bridge packages change only in version; update the controller and browser-node images to get it.

### Upgrade notes

- **On by default.** `PRE_REQUEST_BLOCKING` defaults to true. The gate runs whenever `ALLOWED_HOSTS` refuses
  something: not when it is empty or contains `*`. `PRE_REQUEST_BLOCKING=false` restores 1.11.2's behaviour.
- **What a stopped navigation answers.** It answers `browser_action_blocked` (REST 403, `retryable: false`), with a
  message and `url` that name only the refused origin. That covers:
  - `navigate`;
  - a session's `start_url`, including one that leaves for a refused host while it settles;
  - a tab opened onto a URL;
  - a shadow browse.

  In 1.11.2 a refused `navigate` already answered `browser_action_blocked` ("Action blocked by policy"). A
  `start_url` or a new tab's URL answered `not_permitted` (REST 403 `{"detail": "Not permitted"}`), and still does
  when the page is refused only after it commits. A URL refused as written is refused before anything is sent, as
  before. A click whose navigation was stopped succeeds and reports `target.blocked_navigations`.
- **Two new answers.**
  - A browser the gate cannot attach to gets no sessions: `pre_request_gate_unavailable` (503, retryable).
  - If the gate detaches from a browser that is still running, a replacement attaches and that browser's sessions
    are closed. The next call on one answers `session_closed` with `reason: "request_gate_lost"`, over REST and MCP.
- **Attach modes** (`CDP_CONNECT_URL`, `browser.cdp_attach`).
  - **The switch.** Start that Chrome with `--disable-features=BlockOriginHeaderModificationOnRedirect` and set
    `CDP_BLOCK_ORIGIN_ON_REDIRECT_DISABLED=true`. Without the switch, the controller stops cross-origin 307/308 POST
    redirects in its own sessions, and Chrome fails the same redirects in the operator's own tabs while the
    controller is attached.
  - **The operator's tabs.** Every navigation in that Chrome, the operator's included, waits on the gate's reply
    (about 13 ms). Rarely, a navigation to a refused host fails closed in a tab whose frame the gate cannot
    classify.
- **Saved auth profiles.** Third-party storage partitioning is back on in the browsers the controller launches, as
  in Chrome. A third-party iframe's `localStorage` is now keyed to the top-level site, so a saved auth profile no
  longer carries it. Cookies and first-party logins are still carried.

### Added

- **Pre-request blocking.** One browser-level DevTools Fetch session per browser fails the main-frame request of a
  controller session's page when `ALLOWED_HOSTS` refuses its URL, every redirect hop and a popup's first request
  included. Iframes, contexts the controller did not create (an attached Chrome's own tabs) and allowed URLs go
  through.

  The guard that refuses a page after it commits stays on behind the gate, and still catches what the gate cannot
  see:
  - speculation-rules prefetch and bfcache restores in attach modes, and prefetch in a launched browser where it
    could not be turned off (`/readiness` warns);
  - a same-site refused iframe's service worker serving a popup's first navigation, or one the popup makes before
    its bypass is set;
  - `blob:` navigations a refused iframe makes;
  - requests held when the gate detaches, or started before its replacement attaches.

  Iframes and subresources of other hosts are still not filtered. See
  [`docs/deployment.md`](./docs/deployment.md#stopped-before-the-request-is-sent).
- **Audit and tabs.** Stopped navigations are audited as `navigation_blocked` with `source: "request"`, and the tab
  list marks a stopped tab with `blocked_navigation`.
- **Readiness.** `/readiness` and MCP `browser.readiness_check` report a `pre_request_gate` check. It warns at worst;
  it never fails readiness.
- **`CDP_BLOCK_ORIGIN_ON_REDIRECT_DISABLED`,** for an attached Chrome started with
  `--disable-features=BlockOriginHeaderModificationOnRedirect`.

### Changed

- **Prefetch.** While pre-request blocking is on, the browsers the controller launches turn speculation-rules
  prefetch off when they are acquired.
  - A runtime preference, read back and reported by `/readiness`, is the verified control.
  - The browser-node image also carries the `NetworkPredictionOptions` managed policy as a second layer. It is
    installed at both policy paths, because which one that Chromium build reads is unverified.
- **Service workers.** While pre-request blocking is on, pages the controller owns bypass them.
- **Storage partitioning.** Third-party storage partitioning is back on in the browsers the controller and
  browser-node launch, whatever the setting. Playwright turns it off by default.

## [1.11.2] — 2026-10-29

A security release over 1.11.1, with four advisories:

- GHSA-3pv7-p7w6-q93q (high): a web page could reach the browser desktop through VNC on port 5900,
  bypassing noVNC's Origin check.
- GHSA-9wxr-2vcc-9x6c (moderate): governed approvals could be forged for a tool call, reused after their
  action started, or skipped by the cron tools.
- GHSA-j5hf-3vw7-g4cp (moderate): content of a page outside `ALLOWED_HOSTS` reached the agent through
  popups, logs, downloads, cookies and the pages it chose.
- GHSA-xq9j-p4rw-q2cq (moderate): a page could stop a session's witness receipts; each later action ran,
  then reported an error.

Two more security fixes, to witness verification and to staged skill candidates, have no advisory of
their own, and Fixed lists the rest, among them three that keep operators under named credentials out
of each other's MCP sessions, session ids and files.

To deploy the fixes:
- **update the controller**: its image, or your source checkout followed by
  `pip install -r controller/requirements.txt`, which now pins `idna`;
- **update the browser-node image**, which carries the VNC fix, and take the new `docker-compose.yml`,
  which no longer publishes 5900;
- if `ISOLATED_BROWSER_IMAGE` names an image explicitly, point it at the 1.11.2 browser-node image;
- close isolated sessions started before the upgrade, and recreate the containers.

The client, LangChain and MCP bridge packages change only in version; updating them alone does not
deploy the fixes.

Upgrade notes:
- **A VNC viewer pointed at port 5900 no longer connects.** Open noVNC on 6080 instead
  (`http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale`, or the session's `takeover_url`),
  which asks for `VNC_PASSWORD` when you set one. `VNC_PORT` is removed from `.env.example`, the compose
  file and `make doctor`, and a new isolated session's `runtime` object has no `vnc_port` (the stored
  record of an earlier session keeps it). See `docs/deployment.md`, "Gateway recommendations".
- **Ask again for a governed tool call that was pending or approved when you upgraded.** An approval
  stored before 1.11.2 counts as a browser action's, which no tool call can spend: the agent repeats the
  call without its old `approval_id` (an approved one once you know it did not already take effect), and
  an operator approves the new request. A pending one left from before shows in the queue as a
  `request_human_takeover` action: reject it. With `AUTONOMOUS_APPROVALS=true`, that rejection stops the
  agent approving its repeated call itself only for a call that names no session: a session's rejection
  binds only within that session, which the upgrade's restart ends, so the agent approves the repeat in
  its new session unless you turn `AUTONOMOUS_APPROVALS` off.
- **An approval is spent when a call claims it, before its action runs.** If the action then fails,
  retryable refusals such as `element_not_stable` included, the same `approval_id` is refused, where
  1.11.1 let it run the action again: "was spent when its action started (...), so the action may have
  taken effect: observe the page, or check the outcome, before acting again. Only to do it again, repeat
  the call without approval_id, which asks an operator for a new approval". A spent approval does not
  mean its action failed: check what it did before you approve a repeat. Only a browser action that ends
  before it begins gives its approval back (its session closing, or an upload's file going, after the
  claim included), and the answer then carries `approval_given_back`: retry an `approved` one with the
  same `approval_id` once the cause is gone, except when the note says its session has closed: nothing
  can run it then, so repeat the call in a live session without `approval_id`. Which endings give back,
  and the REST status of each refusal, are in `docs/mcp-clients.md`, "When an approval is spent, and
  when it is given back".
- **A reject sent after a browser action claimed its approval, but before the action began,** stops
  the action (`approval_rejected`) and answers 200 with the record, still `executed`; the approval then
  becomes `rejected`. Once the action has begun, a reject is refused (a 409 over REST). A decision
  sent to another worker or replica while an action holds the approval is refused with a 409 ("has
  already been spent"), where 1.11.1 accepted a reject. See `docs/mcp-clients.md`, "Rejecting or
  approving while an action holds its approval", which also covers Windows hosts with the default file
  store.
- **A governed upload's file is checked before anyone is asked to approve it**, in
  `browser.execute_action` and the agent loop: a missing file is refused (`not_found` over MCP) and
  queues nothing. `browser.execute_approval` and `POST /approvals/{id}/execute` check it before they
  claim the approval, so a file gone since the approval leaves it approved.
- **An agent can no longer ask for approval of a `request_human_takeover` or `done` action, or run
  one**: it is refused with a 400 before anything is queued. `POST /approvals/{id}/execute` answers a
  409, without spending the approval, for a tool call's approval and for a `done` or a takeover approved
  before 1.11.2, each of which 1.11.1 answered there with a 400 ("Unsupported action" over MCP).
- **Approval webhooks and SSE approval events carry `origin`, a server-written `action_summary` and,
  for a tool call, `arguments_size`**, and their `reason` goes out on one line: a newline as `\n`, a
  tab or another control as its code point (`[U+0009]`). A sensitive `type` reads "text hidden at the
  agent's request (sensitive)" in the queue, the confirm dialog, the webhook and the SSE summary, where
  the dashboard read "(sensitive text hidden)": the agent can mark any typed text sensitive.
- **A governed tool call's arguments are kept whole beside its approval** (`<id>.arguments` in
  `APPROVAL_ROOT`, credentials left out), for `GET /approvals/{id}/arguments` and the queue's "View all
  N characters". The file goes when the call that spends the approval ends, or at the next start if the
  controller stopped during that call. A rejected one's goes once the rejection is older than
  `APPROVAL_TTL_MINUTES` (at the next approval request or start), and until then an operator can still
  approve it. One left pending, or approved and never run, keeps it until a call spends it, and nothing
  else sweeps it. See `docs/mcp-clients.md`, "What the operator is shown".
- **An approval an earlier version stored whose action 1.11.2 refuses is left out** (a navigate to a URL
  whose host a browser reads otherwise, below): listings leave it out, and fetching or executing it
  answers as an unknown id, so nothing can execute it, and the agent asks again. Fetching or listing it,
  from either store, logs a warning that names it and where it fails, never what it holds (a navigate
  URL's query can carry a token).
- **The cron tools ask for approval under governed approvals.** An agent that creates, triggers or
  deletes cron jobs with `workflow_profile=governed`, or on a controller with
  `FORCE_GOVERNED_APPROVALS=true`, now gets `approval_required`, and repeats the call with the
  `approval_id` once an operator approves; REST `POST /crons` and `POST /crons/{id}/trigger`, which need
  the bearer token, never ask. A job created by an earlier version keeps running as `fast`, and jobs
  cannot be edited: to govern one, delete it and create it again from a governed caller, or through
  `POST /crons` with `"workflow_profile": "governed"`, or turn on `FORCE_GOVERNED_APPROVALS`, which
  forces every run to `governed`. A governed job's run cannot continue past an action that needs an
  operator's approval: it stops there, and its session closes with the run, so that approval, once
  approved, can no longer be executed: it stays `approved`, and triggering the job again asks for a new
  approval in a new session. With `AUTONOMOUS_APPROVALS=true` the agent approves such an action itself
  and the run carries on. Run work that needs an operator's approval in an interactive session, or keep
  the job `fast` under a reviewed script. See `docs/mcp-clients.md`, "Cron jobs and
  FORCE_GOVERNED_APPROVALS".
- **A page `ALLOWED_HOSTS` refuses is read again only after the agent navigates it to an allowed URL, or
  a blocked action's rollback brings it back.** Until then observe, screenshots, the page-reading MCP
  tools and actions other than navigation refuse it (REST: 403), also after the controller has sent it
  back to `about:blank`, where they used to read the blank page, and after it has loaded an allowed URL
  on its own. A popup or blank window a refused page opened is refused with it. `go_back` leaves a
  refused page refused, where 1.10.0 through 1.11.1 let it leave a disallowed page; `reload` and
  `go_forward` are refused there, as before. A refused tab that is not the active one cannot be
  activated: close it, or open a new tab. See `docs/deployment.md`, "Reading a refused page again".
- **While a tab of the session can run a refused host's code, and for 2 seconds after it closes, the
  other tabs' own navigations are refused too.** Such a tab was refused for where it went, a refused
  host's redirect or its opener, is navigating through a refused host, or, refused only because of
  another tab or still waiting for its opener, is on a `blob:` or `data:` page or a blank one the
  controller did not send it to. A tab that loads a page on its own meanwhile (a link the agent clicks
  while the refused tab is open, a redirect, a script, a reload) is sent back to `about:blank`, and
  whatever it had loaded, a form in progress included, is lost. Close the refused tab if it is still
  open, then navigate the other one: the refusal says so. Each of the agent's actions on the session (a
  navigate, a click, typing, a key press), and opening a tab onto a URL, first waits out what is left of
  those 2 seconds, for at most 2 seconds from the call, and not while a refused tab is still open; the
  wait comes before the action begins, so a call cancelled meanwhile gives a claimed approval back. A
  tab refused this way that is not the active one cannot be activated, also once the refused tab is
  closed ("That tab loaded while another tab of this session was refused, or within 2 seconds after a
  refused tab closed, ..."): close it, or open a new tab. See `docs/deployment.md`, "Other tabs while
  one is refused".
- **A navigation to a URL that names another host to a browser than it shows is refused**, in every
  profile: a `navigate` action, `POST /sessions/{id}/actions/navigate`, `POST /sessions/{id}/tabs/open`,
  and a new session's or a fork's `start_url`. That is a backslash in its authority
  (`https://evil.example\@bank.example/`), a URL written without the `//` after its scheme or with extra
  slashes (`https:u:p@host/`), a percent-encoded host, an IPv4 address not written as a dotted quad
  (`http://127.1/`), a host a browser maps to another name beyond case folding and composition (one in
  full-width digits, say), or a control character (U+0000 to U+001F, a tab or a newline among them) in
  its scheme or host, which a browser drops. A backslash or a control in the path or the query is
  accepted. Each run of a cron job an earlier version stored with such a `start_url` fails before a
  browser opens, and the controller logs why: delete the job and create it again. See
  `docs/mcp-clients.md`, "What the operator is shown".
- **Downloads and `browser.get_cookies` follow `ALLOWED_HOSTS`.** A download must come from a host on
  the list, through no off-list host on its redirects, and from a page on one, or it is cancelled and
  recorded as `blocked`. The host checked is the file's after its redirects, so a file an allowed host
  redirects to (a CDN, a file host) is refused unless that host is listed too, and listing it also lets
  pages navigate there; a link that passes an unlisted redirector (a tracking link, say) is refused even
  when the file's host is listed. A file a page generates (`blob:`, `data:`) is judged by the page that
  reports it. `browser.get_cookies` returns only cookies a listed host would receive; with
  `ALLOWED_HOSTS` empty or `*`, it filters nothing. See `docs/deployment.md`, "Refused downloads" and
  "Cookies an allowed host would receive".
- **The page-reading MCP tools and the REST CDP routes take the session lock, and are bounded.**
  `browser.eval_js`, `browser.get_html`, `browser.find_elements`, the storage tools, `browser.drag_drop`
  and the `browser://<session>/dom` resource wait up to 10 seconds for a session another call holds and
  then fail, and give up on a page that does not answer within 30 seconds (a `browser.find_elements`
  text query within 10, `query_timeout`; `browser.eval_js` takes a `timeout_ms` of up to 60000 instead;
  a drag adds `ACTION_TIMEOUT_MS` for each end named by a selector or `element_id`, then holds the
  session up to 7 seconds more to remove its scroll watch and release the mouse; giving up does not stop
  the page's script). `browser.wait_for_selector` does not take the lock, and is refused if its page
  became refused, closed or not the active tab while it waited. The REST CDP routes (`GET
  /sessions/{id}/cdp/element`, `POST /sessions/{id}/cdp/raw`) answer 409 when they cannot get the
  session within 10 seconds, 403 for a page `ALLOWED_HOSTS` refuses, 503 when they cannot attach to the
  active tab, where 1.11.1 used the tab they were attached to before, and 504 when the browser does not
  answer, attach or detach within 10 seconds. See `docs/deployment.md`, "Reads and the page tools".
- **Witness verification fails on missing evidence, where it used to pass it.** The first start after
  upgrading anchors every chain that has no anchor yet, as the chain stands, and marks the witness root
  (`.anchors-migrated`); no later start does. That includes a chain whose anchor was deleted on 1.11.1
  or earlier, so if its evidence matters, check the chains without a `<id>.jsonl.anchor.json` before
  that start. With `WITNESS_ENABLED=false`, a session with no receipts now verifies `false` ("no
  receipts recorded for this scope"), and the `anchor.status` value `unanchored` is gone. See
  `docs/deployment.md`, "Witness receipts: what a signature proves".
- **Skill candidates staged by an earlier version are refused** when read or registered ("... induce it
  again"), and `harness.list_candidates` leaves them out, logging a warning for each: run the
  convergence again to stage them afresh. This only matters on a controller with a mesh identity, where
  candidates are signed.
- **Under named credentials, a subfolder of `UPLOAD_ROOT` named like a session id** (12 lowercase
  hexadecimal characters, so a 12-digit timestamp too) is treated as that session's directory, even if
  you made it yourself. An upload refused for that reason says so: rename the folder.

### Security

- **Raw VNC is gone: x11vnc listens on no TCP port, and noVNC on 6080 is the only way to the desktop.**
  x11vnc served the browser-node display on TCP 5900, with no password unless `VNC_PASSWORD` was set,
  and its VNC library accepts WebSocket connections there, checking only that an `Origin` header is
  present. So a web page in a browser on the same host could reach the desktop around noVNC's Origin
  check, through the base compose file's `127.0.0.1:5900` publish or the host port each isolated session
  container published at `ISOLATED_BROWSER_BIND_HOST`, wherever that browser lets pages connect to local
  addresses without asking. The agent's own Chromium shares the container's network and needed no
  publish. In images from 1.5.1 on, which install it with Playwright 1.62, it is expected to ask before
  a public page connects to a loopback address (not tested); older images install an older Chromium,
  which may not ask. A page that got through had full view and control of a browser holding stored
  logins. Every release through 1.11.1 is affected. x11vnc now serves RFB only on a unix socket, in a
  directory only the browser user can enter, and the IPv6 port its library opened on 5900 by itself is
  closed too; websockify forwards noVNC to that socket, and nothing exposes or publishes 5900.
  Unchanged: noVNC has no password by default, and a program that is not a browser can send any Origin
  it likes, so anything on the host or the Docker network that can reach 6080 can still take over the
  desktop. Set `VNC_PASSWORD` wherever that matters.

Governed approvals:

- **An approval for a governed tool call can no longer be minted as a browser action, or spent as the
  wrong one.** A governed tool call that is not a browser action (`browser.eval_js`,
  `browser.set_cookies`, ...) is approved through a stand-in `request_human_takeover` action whose text
  names the tool and a digest of its arguments, and approvals were matched on field values alone. An
  agent could ask for that stand-in through `browser.execute_action`, with a reason that previewed other
  arguments, which the dashboard showed as that tool call, and once an operator approved it, spend it on
  the arguments the digest named. Every release from 1.0.5 through 1.11.1 is affected (before 1.8.0 the
  stand-in carried no digest, so such an approval covered any governed tool call with the same risk
  category in its session). An approval now records its origin, `action` or `tool_call`, set only by the
  gateway: one origin's approval is refused for the other, a pending one is reused only within its
  origin, and the dashboard names a tool call only for `origin: tool_call`. An operator's rejection
  counts whatever the origin, so one made before the upgrade still holds where it binds (upgrade notes),
  unless it was made before 1.8.0, on a stand-in with no digest, which matches no current call. Still
  the case: the agent writes an approval's reason, so read the action as well as the reason.
- **An approval that let its action start is spent, whatever fails after.** An approval was consumed
  only once its action had returned, so an action that went through and then failed (the settle after a
  click, the observation, a captcha), or a controller restart while it ran, left it approved, and the
  same `approval_id` ran the action again: a second payment click or upload. So did a governed upload
  with `REQUIRE_APPROVAL_FOR_UPLOADS=false`, and an upload whose file went in while Playwright's call
  raised. Every release through 1.11.1 is affected. The claim now spends it, only a browser action that
  ends before it began gives it back, and an action approval covers one operation. With `STATE_DB_PATH`
  set, an approval spent while SQLite was unavailable could also run again once SQLite was back. At a
  start on SQLite, SQLite's spent record now always wins over the file store's, and an operator's
  rejection over a file record not updated after it; SQLite takes the rest of what it missed, and the
  record it keeps is written back to the file store, so a later start whose SQLite fails cannot claim
  the approval again. If that write fails, SQLite stays primary, and if SQLite then cannot take the rest
  either, the controller refuses to start rather than run on a file store that could claim a spent
  approval. Every record write lands whole under the store's lock, and one whose SQLite half fails puts
  the file record back before it reports the failure, so the next start does not revive what the caller
  was told failed. A give-back reported as failed stays failed even when putting the file back fails
  too, since SQLite's spend wins. Not covered: two controller processes sharing one approval store (file
  or SQLite) can still both claim an approval, since the claim is atomic only within a process; several
  workers or replicas are not supported. The details, the approval guard's error codes and witness
  receipts included, are in `docs/mcp-clients.md`, "When an approval is spent, and when it is given
  back".
- **An approval whose session has no record follows the operator who asked for it.** Who may use an
  approval follows its session's owner, and a session with no record (lost while the approval stayed)
  read as unowned, so under named credentials any operator could read, list, decide and execute another
  operator's approval, its reason included, which previews a tool call's arguments (credential fields
  left out). Such an approval now belongs to the operator its record names, and one that names nobody is
  refused under named credentials, as such a session id is (Fixed, below); a known session that nobody
  owns stays open to every operator.
- **What an operator is shown for an approval says what it covers.** The webhook and the SSE event
  carried neither the origin nor the action, so a coordinate click whose reason copied a tool call's
  read the same as that tool call's approval. The queue showed a coordinate click as a bare `click`, an
  option pick without its value, and a selector where an element id beside it was what ran, and cut
  typed text at 200 characters without saying so. A tool call's preview hid every value under a key
  named `value`, `password`, `token` or `secret`, which hid a harness run's start URL and conditions,
  and its URL fields were accepted even where a browser reads another host in them than the controller
  does. Now the queue, the summary, the webhook and the SSE event list every field an action approval
  binds and mark one that does not act, nothing is cut without saying so, and characters that hide or
  reorder text (bidi overrides, zero-width marks, controls) are shown as their code point (`[U+202E]`).
  A tool call's preview leaves out credentials by tool and path, among them the userinfo and
  credential-named query values of five URL fields: `browser.cdp_attach`'s URL, a cookie's URL, a
  harness run's start URL and mock final page URL, and a cron job's `start_url`. Those five are refused
  where a browser could read another host in them than the controller: a backslash or a control
  character (U+0000 to U+001F) anywhere, a percent-encoded host, one UTS 46 maps to another name beyond
  case folding and composition, an IPv4 address not written as a dotted quad, or a URL written without
  the `//` after its scheme or with extra slashes. A navigate's `url` is refused by its host alone
  (upgrade notes), and the queue and the summary show it with its host as a browser sends it (`xn--`),
  the host as written beside it. Witness receipts and audit entries carry an approval's origin and, for
  a tool call, the tool, where they named only the stand-in; the audit log stays operator-wide, so under
  named credentials every operator reads these. Still the case: letters that look alike across scripts,
  and right-to-left letters, are shown as they are; a credential in a navigate's URL is shown, and so is
  one in a URL's fragment, in a query parameter of another name, or anywhere but those paths; and with
  `AUTONOMOUS_APPROVALS=true` the agent approves its own calls, and an operator's rejection stops it
  approving the same request only within that session (in controller scope, for the same operator). The
  full rules are in `docs/mcp-clients.md`, "What the operator is shown" and "Controller-scope approvals
  and the audit log".
- **The cron tools now ask for approval under governed approvals.** `browser.create_cron_job`,
  `browser.trigger_cron_job` and `browser.delete_cron_job` were never gated, and every run of a job
  started as `fast` unless `FORCE_GOVERNED_APPROVALS` (1.10.0 and later) was on, so a governed agent
  could plant a job that ran its writes without approval. Every release from 1.0.5 through 1.11.1 is
  affected, for an agent served the cron tools (only the `full` profile has them, unless
  `MCP_TOOL_PACKS` adds the `cron` pack). The first two are now `write` and the third `destructive`, and
  every run of a job starts under the `workflow_profile` of the call that created it. A run or step
  queued by a governed `browser.queue_agent_run` or `browser.queue_agent_step` call now runs governed
  too, whatever its request names, where it ran under the request's own profile, `fast` included. `POST
  /crons` stores the `workflow_profile` its body names (`fast` by default). The full tool profile now
  has 19 tools with a governed kind, not 16. Still the case: tools without one are not governed, the
  proxy persona tools among them, and `browser.create_session`, `browser.fork_session` and each run of a
  cron job, scheduled or triggered, open a `start_url` with no approval, though creating or triggering a
  job through the MCP cron tools asks for one (REST `POST /crons` and `POST /crons/{id}/trigger` never
  do). See `docs/mcp-clients.md`, "Cron jobs and FORCE_GOVERNED_APPROVALS".

`ALLOWED_HOSTS`:

- **A popup onto a host off `ALLOWED_HOSTS` is sent back to `about:blank`, and a page is refused for
  where it came from.** Playwright reports a popup only once its first navigation has committed, so the
  listener that holds a page to the list came too late. A popup opened straight onto a disallowed host
  (by `window.open`, a `target=_blank` link, or a page timer) stayed there, kept running, wrote no
  `navigation_blocked` event, and fed its console messages and page errors into every observation and
  the console tools; shadow-browse sessions attached no listeners to their popups until their tabs were
  next listed. A blank window a refused page opened is same-origin with it and scriptable by it, so
  whatever the page had it log, fetch, title or download passed every URL check, and the agent could
  switch to it and observe it. Every release through 1.11.1 is affected: the guard that missed these
  arrived in 1.10.0, and before it no popup was sent back at all. The controller now checks the URL a
  page is already on when it attaches its listeners, to every popup in shadow-browse too, and drops a
  refused page's console messages and page errors. A page is also refused while it is tainted: it
  committed a refused URL, came through a refused host's redirect, loaded a page on its own while
  another page of the session was refused or within 2 seconds of its closing, or is a popup of a refused
  page. The TOTP autofill skips a refused page, also on a host in `totp_hosts`. The full rule is in
  `docs/deployment.md`, "When a page is refused".
- **The network log and request failures withhold what a refused page asks for.** A request made by a
  page `ALLOWED_HOSTS` refuses, or one that navigates a top-level page to a refused host, was logged
  whole, the page's HTML included, and so were a service worker's requests after the refused page that
  registered it was blanked; `request_failures` kept the full URL of each failed request such a page
  made. Such requests are now recorded as `"withheld": "page off ALLOWED_HOSTS"`, cut to their origin,
  with no headers or bodies, and so are a popup's first navigation, a top-level navigation that
  starts while a page of the session is refused, and a request the controller cannot attribute. A
  refused page's failed requests are left out of `request_failures`. Iframes and subresources an
  allowed page loads from other hosts are logged as before. See `docs/deployment.md`, "Logs of a
  refused page".
- **A download from a host off `ALLOWED_HOSTS`, or one a refused page starts, is cancelled, and
  `browser.get_cookies` returns only what a listed host would receive.** Such a download, a file a
  refused page generated itself included, was saved and readable through `browser.read_download` and its
  artifact URL, and `browser.get_cookies` returned the cookies a page off the list set before it was
  blanked. A download's final URL, any off-list host its navigation passed, and the page it is reported
  on are now checked; nothing of a refused one is saved, and it is recorded as `blocked`, with its
  source's origin only, and a `download_blocked` audit event. `browser.get_cookies` judges host-only,
  domain and partitioned cookies as the browser sends them. See `docs/deployment.md`, "Refused
  downloads" and "Cookies an allowed host would receive".
- **A read is refused when its page went refused, closed or stopped being the active tab while it was
  read, and a refused page's URL is shown by its origin only.** Reads checked the page's URL before
  they read it, and some again after, so a page that went to a refused host and came back, or an active
  tab that closed and handed the session to another, passed both checks and was read. The REST CDP
  routes read the active page whatever its host, and tab listings, session summaries, the snapshots an
  action returns, approval requests and the `navigation_blocked` event showed a refused page's URL
  whole. Each read now notes the active page and its count of refused commits first, and refuses
  afterwards if either changed, the page closed, or the page is refused now; the CDP routes also take
  the session lock and answer 403 as observe does, and those places now show a refused page's URL
  as `scheme://host:port`. The 1.10.0 note that raw CDP passthrough is unchanged no longer holds. See
  `docs/deployment.md`, "Reads and the page tools".
- **What a page `ALLOWED_HOSTS` refuses still does.** Its request has already gone out when it is
  refused, with whatever the page put in its URL, and the page runs, sets cookies and shows in the
  takeover (noVNC) view until it is blanked, which waits for any call in flight on the session. A
  service worker it registered keeps running after that. What an allowed origin's own scripts do with
  what a refused redirect or a refused opener put in its URL is not tracked. A refused page can set a
  cookie on a parent domain that an allowed host receives, and `browser.get_cookies` returns it. A
  withheld network entry keeps the host's labels, which the page chooses. Playwright traces
  (`ENABLE_TRACING`, off by default) record the refused page too, and a storage-state export holds every
  host's cookies. Iframes and subresources from other hosts on an allowed page are not filtered, by
  design. As before, an internationalized host matches `ALLOWED_HOSTS` only in its `xn--` form: list it
  that way and navigate to its `xn--` URL (the Unicode form fails closed: the page is refused, not
  read). The full list is in `docs/deployment.md`, "What a refused page still does".

Witness receipts and skill candidates:

- **A page can no longer stop a session's witness chain from recording.** Two faults ended recording,
  both since 1.7.0. The tail read that finds the last receipt decoded bytes from an offset as text, so
  once a chain passed 64 KiB and that window began inside a multi-byte character (a Japanese page, a
  euro sign), every later append failed. And a receipt holding U+2028, U+2029 or U+0085, which a page
  can put in its title, was cut in two by the line splitting, so every later append failed at any chain
  size, and verify, the receipt list and the bundle export failed too. Each later action ran, then
  reported an error, and its receipt was never recorded, so a caller that retried could repeat a click
  or a submit. The tail is now read as bytes and chains are split on newlines only; a chain stuck this
  way verifies, lists and exports again with no repair, but the upgrade's restart ends its session, and
  the receipts that were never written cannot be recovered.
- **Witness verification no longer reports missing evidence as valid.** Anyone who could write the
  witness directory could remove receipts and still read `valid: true`: a missing or corrupt anchor
  read as `unanchored`, which passed, so cutting a chain's tail and deleting its anchor hid the cut,
  and so did the next receipt, which moved the anchor to the shortened chain. Now `valid` is `false`
  when the chain is gone or empty or its anchor is missing or unreadable, the chain must match the
  anchor exactly, and a later receipt no longer moves the anchor over a chain it does not describe.
  Still the case: someone with write access to the witness directory can make a cut chain verify, by
  rewriting the anchor, by deleting the chain with its anchor (the next receipt starts a new chain that
  verifies), or by deleting `.anchors-migrated` before a restart (which anchors a cut chain whose
  anchor is missing as it stands). For evidence that must survive that, pin a head hash somewhere the
  controller host cannot write. See `docs/deployment.md`, "Witness receipts: what a signature proves".
- **A staged skill candidate can no longer be edited, or copied to another directory, and still read as
  verified.** On a controller with a mesh identity, where candidates are signed, someone who could write
  to the staging directory could change fields of `candidate.json` (`safety_class`, `verifier_backend`,
  `name`, ...), or copy a genuine `candidate.json` next to altered files, and the registry served it as
  verified. The signature now covers every field of `candidate.json` but the envelope itself and the
  `files` map, which is built from the directory the candidate was read from; that directory must be
  named for the candidate's `skill_id` and stay inside the staging directory. Still the case: no check
  covers the review files (`contract.json` and `trace.json` are never compared with the contract and
  trace hashes the envelope signs, and `provenance.json` and `envelope.json` are not hashed). See
  `docs/convergence-harness.md`, "Mesh Boundary".

### Fixed

- **The session-limit error names only the sessions the caller may use.** Under named credentials
  (`API_BEARER_TOKENS`), the error `browser.create_session` (and an observe or act tool that creates a
  session) returns at `MAX_SESSIONS` listed every live session id, other operators' included. It now
  lists the caller's own and unowned sessions, and counts the rest.
- **An MCP session (`MCP-Session-Id`) belongs to the operator who initialized it.** Under named
  credentials, another operator holding the id could ping it, subscribe it to resources and
  `DELETE /mcp` it, though tool calls always ran as the caller's own operator. Any other operator now
  gets `-32001` "Unknown MCP session". MCP session ids created before this release, or by a client
  that initialized without a named credential, stay usable by any operator until they are deleted or
  evicted. The `browser://sessions` update no longer reaches other operators' subscribers when an owned
  session closes. The session table keeps 500 sessions and evicts the least recently used, so one
  operator initializing many evicted another's idle session; an operator now keeps at most 100, and
  past that loses its own least recently used first. Unowned sessions are bounded by the 500 alone.
- **Under named credentials, a session id with no live session and no record is refused like another
  operator's.** The routes and MCP tools that read a session's files by id (replay, audit, witness)
  served an orphaned id's files and events to every operator, and other routes answered it in words that
  told another operator's session from none. Routes and tools that name the session id now answer `404`
  `unknown_session` before anything else. The chain of the auth-profile receipts (`auth-profiles`),
  which has no session record, is refused the same way: its witness routes and tools answer `404` to
  every operator. Its agent jobs, and its approvals that name no operator, are left out of listings and
  answer as if they did not exist, and an upload from its directory under `UPLOAD_ROOT` is refused
  (upgrade notes); other subfolders stay shared with every operator. Deployments without named
  credentials are unchanged.
- **Tab listings, the `fast` observe preset and the bot-challenge check no longer wait without end for
  a page stuck in a script.** All three run under the session lock, so one tab whose renderer never
  answered stalled every call on the session. A tab's title now waits at most 2 seconds (the tab is
  listed without one), the `fast` preset's title at most 30 seconds (observe fails), and the
  bot-challenge probe that runs after every action at most 30 seconds (the page is then checked by its
  URL alone).
- **An auth-profile save that fails before its files are in place leaves the profile as it was, unless
  putting the previous state back fails too.** A save wrote the state file first and then read the
  page's title for the profile's metadata, so when that read or write failed the new state stayed in
  place, on a first save with no metadata and so no owner. The title is now read first, and the new
  state and metadata are put in place together; a failure after that (recording the save, say) is
  reported as an error, and the new profile stays. A process killed during a save can still leave the
  new state beside the previous metadata, or the new metadata beside the previous state, and hidden
  files whose names start with `.state.json` or `..state.json`, holding the previous login and the new
  one. A first save, and a named operator's re-save of a profile whose metadata names no owner (saved
  under the shared token or before owners), now puts its metadata, which names the owner, in place
  before its state: one killed between the two leaves at most that metadata beside no state or the
  previous one, and one whose state cannot be put back keeps the new metadata beside the new state, so
  neither leaves a new login with no owner that every operator could open under named credentials. See
  `docs/deployment.md`, "Auth profiles".
- **A policy block after a `go_back` or a `reload` is no longer rolled back with another `go_back`,**
  which went one page further back, or to the page before the reloaded one. The page stays where the
  action left it, and one `ALLOWED_HOSTS` refuses is sent to `about:blank`.
- **The audit log's trim no longer drops an event whose text holds a Unicode line separator.** It split
  the log at U+2028, U+2029 and U+0085 as well as at newlines, so such an event came back as fragments
  that no longer parse, and listings left it out for good. Text a page chooses reaches the log, a
  download's file name among them. The trim now splits on newlines only, and so does the convergence
  harness's trace, where an event holding one failed the run.
- **A navigate to a host `ALLOWED_HOSTS` refuses is refused before an approval is asked for.** An
  operator was asked to approve a navigation that could never run, since `navigate` checked the list
  only once it ran. A host that leaves the list after the approve is refused when the approval is
  executed, and the approval is given back.
- **An action whose observation hits a permission error on a file reports it without the server's
  path.** The OS error, path included, was returned as the action's `observation_error` (`EACCES` or
  `EPERM`: a screenshot in an artifact directory the controller may not write, say). It is now a fixed
  text, and the OS error is logged.

## [1.11.1] — 2026-10-26

A security release over 1.11.0, with one advisory: GHSA-wrg7-3hw9-f5wj (a page could choose which
element an `element_id` acted on). **Update the controller** (its image, or your checkout of
`controller/` if you run it from source). The client, LangChain and MCP bridge packages change only
in version; updating them alone does not deploy the fix.

Upgrade notes:
- **Observe again after updating.** Ids from observations made before 1.11.1 (`op-` and up to 8
  characters) are refused as `invalid_element_id`, and so are their `selector_hint`s passed as a
  `selector`.
- **`browser.find_elements` and `browser.wait_for_selector` refuse a `selector_hint`**
  (`selector_hint_not_a_selector`). Pass the `element_id` to the action tools instead, or a CSS
  selector of your own.
- **Some actions that used to run are now refused**, below: a click or hover by `element_id` whose
  point lands on an embedded frame inside the element, and a hover by `element_id` on an element
  something covers (1.11.0 moved the pointer there anyway), both `element_covered`; a selection by a
  `<select>`'s own `element_id` while it never stops moving (`element_not_stable`); and a drag to or
  from a CSS end with no box (`drag_end_not_visible`). A click on an input that a label for it
  covers, such as the hidden input of a custom checkbox, failed on 1.11.0 too, after
  `ACTION_TIMEOUT_MS`; it is now refused at once, with the way out.
- **New error codes** a caller may see: `invalid_element_id`, `stale_element_id`,
  `unknown_element_id`, `element_not_visible`, `element_disabled`, `element_not_stable`,
  `element_not_in_view`, `element_covered`, `option_not_found`, `option_disabled`,
  `element_not_a_select`, `element_not_a_file_input`, `element_target_moved`, `change_unconfirmed`,
  `page_not_responding` (HTTP 504 over REST), `drag_ends_not_in_view`, `drag_page_scrolled`,
  `drag_end_not_visible` and `selector_hint_not_a_selector`. Of these, the refusals from
  `element_not_visible` to `element_not_a_file_input`, and `stale_element_id` and
  `unknown_element_id`, are marked retryable: observe again before retrying. The rest are not.
- **An upload whose file went in is reported as done.** By `element_id`, read `target.delivery` and
  `verification.verified` to see whether the input was confirmed to hold the file, rather than
  retrying; by selector, `verification.verified` means only that the transfer returned.

### Security

- **A page can no longer choose which element an `element_id` acts on.** Observe wrote each id into the page
  as a `data-operator-id` attribute, kept any such id the page had set itself, and an action found its element
  again with an unescaped `[data-operator-id="<id>"]` selector, as did a `selector_hint` passed as a selector.
  A page could give a hidden "Pay now" button the id of a visible "Cancel", copy an id once observe had
  written it, or be named by a crafted id carrying selectors of its own, and the action ran on an element the
  agent never chose. Every release from 0.1.0 through 1.11.0 is affected. The ids now live in an isolated
  JavaScript world the controller creates for each page, which page scripts can neither read nor change, and
  observe no longer writes element-id attributes. An `element_id` must be in the form observe issues (`op-`
  and 24 lowercase hex digits) or it is refused (`invalid_element_id`). An id from an earlier document (after
  a navigation to a new document, or a reload), or for an element the page removed or moved into another
  document (a same-origin frame's), is refused as `stale_element_id`, and one the tab was never given as
  `unknown_element_id`; past the last 10,000 ids a tab was issued, telling the two apart is best-effort.
  Observe again and use an id from the new observation. Ids from observations made before the update no longer
  work.
- **An action by `element_id` acts on that element, not on one the page points it at.** Click, hover, type and
  select now find and check the registered element itself in the isolated world, and aim the mouse or the keys
  at it; they add no attribute or node to the page. The element is scrolled into view and waited on until it
  is visible and stable (the same box in two consecutive animation frames, as Playwright requires; on a page
  that draws no frame at all for 1 s, such as a hidden tab, two checks 50 ms apart count instead, and a wait
  whose frames stop partway is refused as `element_not_stable` at its deadline), for click, hover and type
  until it is inside the viewport, and for click, type and select until it is enabled: up to
  `ACTION_TIMEOUT_MS`, except that type waits up to 1 s and then focuses the field directly. The pointer goes
  to within 2.5 px, in each direction, of the centre of the element's first client rect inside the viewport
  (Playwright's clickable point; a hover goes to the centre itself), and an element covered there is scrolled
  again with another alignment, as Playwright retries; the hit test right before the mouse is pressed (for a
  hover, while it waits) accepts only the element or something inside it, so a label for it, an embedded frame
  in the document or its open shadow roots (inside the element too), or anything over it, is refused
  (`element_covered`, naming what covers it) rather than clicked or hovered (type focuses a covered field
  directly instead); keyboard focus is checked on the element before the text goes out, between select-all and
  delete, and after the clear (`focus_lost`; focus in a frame inside the element, its open shadow roots
  included, does not count, since keys would go to the frame's document). A selection follows Playwright's
  matching rules, and, as Playwright's does, one by the `element_id` of a label, or of an element in a label
  that is not a control itself (Playwright's 'follow-label' rule), is made in the `<select>` the label is for,
  the one found when the action starts, which is the one checked as well. An element that never becomes ready
  is refused with `element_not_visible`, `element_disabled`, `element_not_stable`, `element_not_in_view` or
  `element_covered`; a select with no matching or only a disabled option with `option_not_found` or
  `option_disabled`. Upload still uses Playwright's file transfer. It marks the file input (the element, or,
  as Playwright's transfer does, the control of a label it names by the same rule; anything else is refused as
  `element_not_a_file_input`) with a one-time attribute for that one action, checks before the upload that the
  marked input is the only element marked (`element_target_moved` otherwise), and removes the marker in the
  document and its open shadow roots, and from the input itself. A change sent to the isolated world (a focus,
  a selection, the upload's marker) is never sent twice: when its answer is lost after it was sent, the action
  fails as `change_unconfirmed` (not retryable), since the change may have been made.
- **What an `element_id` guarantees, and what it does not.** The page cannot choose, forge or copy which
  element an id names. It can still redirect an action in the gaps between the checks and the input, as it
  always could with its own scripts: the few milliseconds between the last hit test and the press, and the
  20–80 ms between mouse down and up; focus is not checked between keystrokes, and a keydown handler can move
  focus within the key it handles, the clear's Delete included: the check after the clear then reports
  `focus_lost` after the field it moved to was emptied; hover is not hit-tested once the pointer arrives; a
  drag's destination is not hit-tested (drag libraries put the dragged element under the pointer), and its
  check of a coordinate end compares scroll positions, so a page can still change what is under that point (by
  scrolling a box no element end is drawn inside while stopping its scroll events or inside a shadow root, by
  scrolling back to where it was, or by replacing content), and a scroller an element end is slotted into
  through a closed shadow root is not seen; the hit test and the focus check do not see into closed shadow
  roots, so an embedded frame behind one inside the element takes the click, as it does with Playwright and a
  human click, and focus in such a frame reads as focus on the element, so typed text goes to the frame; and
  between the check before Playwright resolves the marked file input and the check after it, a page script can
  move the marker to another file input of its own document, so that the upload goes there, and then put it
  back. The upload's check afterwards is best-effort (below).
- **No MCP tool or browser action matches a `selector_hint` against the page any more.** `browser.drag_drop`
  takes the registered element for a hint (refusing unknown, stale and malformed ids); `browser.find_elements`
  and `browser.wait_for_selector` refuse a hint (`selector_hint_not_a_selector`) instead of matching an
  attribute only the page could have written. A hint passed as a `selector` to the action tools still names
  its element.
- **A crashed or hung tab no longer hangs observe's element scan or an action's element checks.** Every
  message to the isolated world has a deadline: `ACTION_TIMEOUT_MS`, plus its own wait for a script that waits
  on the element. It bounds each message, not the whole action. A page that does not answer in time fails with
  `page_not_responding` (not retryable): HTTP 504 over REST, and an MCP tool error with the same code and
  `retryable: false`. Its CDP session is detached and replaced on the next call, and one whose attach
  completes after its deadline, or after its call was cancelled, is detached once it does. Other calls still
  wait on the page as before (its title, mouse and keyboard input, and the checks after an action, such as the
  bot-challenge probe), so a page that hangs during an action, in its click handler for example, holds the
  action until it answers.

### Changed

- **The hit test before a click now runs after the 30–120 ms pause before the press**, for selector targets as
  well, so a cover put up during the pause is caught; the press follows the check with no deliberate delay.
- **A click or selection by `element_id` on an element `aria-disabled` disables is refused as
  `element_disabled`, by a port of Playwright's rules:** on an element whose role takes `aria-disabled` (a
  button, link, checkbox, textbox, option and the like), set on it or on the nearest ancestor that sets it
  either way; and, as in Playwright, the check is made on the button a part of it belongs to, or on the
  control of the label it is in. A generic `div` with `aria-disabled="true"` is still clicked, as in
  Playwright. 1.11.0 refused the same cases through Playwright, as a retryable `browser_action_failed` after
  the same wait.
- **A custom checkbox whose visible part is a `<label>` over a hidden input is refused at once when clicked by
  the input's `element_id`** (`element_covered`), where 1.11.0 failed after `ACTION_TIMEOUT_MS` as a retryable
  `browser_action_failed` (Playwright found that the label intercepts pointer events). The refusal names the
  label (or the part of it at the pointer) and the way out: click the label by coordinates, or with the
  selector `label[for="…"]` it gives when the label has a `for` and the whole selector fits in the advice. An
  element whose centre is an embedded frame inside it, which 1.11.0 clicked into, is refused with the advice
  to click a part of it outside the frame by coordinates. Such covers, which waiting cannot remove, are
  refused after one round of re-scroll alignments, not at the end of `ACTION_TIMEOUT_MS` (15 s at the
  default). A refusal after re-scrolling says the page was scrolled; the page is left where the last re-scroll
  put it. The cover a refusal names is bounded (its tag and its id, or its first class, each cut to 40
  characters; the description and the advice at most 200 characters each), whatever the page calls its
  elements.
- **Select by `element_id` waits for the named element's box to settle** before matching options, as a click
  does: a `<select>` named by its own `element_id` that never stops moving, which 1.11.0 selected, is now
  refused (`element_not_stable`). For a label, its own box is the one that has to settle.
- **`browser.drag_drop` with an end named by its `selector_hint` (an element end)** reads every other end once
  the element ends are in place, since bringing one into view can scroll the page or a scrolling element in
  it: an element end again, without scrolling, and a CSS end for the first time, each refused when its point
  is outside the viewport (`drag_ends_not_in_view`); a coordinate end is refused (`drag_page_scrolled`) when,
  afterwards, the window or a box an element end is drawn inside (a web component's shadow scroller included)
  is at another scroll position, an element end is drawn inside other boxes than before, or a scrolling
  element outside shadow roots sent a scroll event meanwhile, since the point may no longer be where it was.
  Both refusals give the way out: a viewport tall enough for both ends (`browser.set_viewport`), or scrolling
  so both are in view. A source named by its `selector_hint` is hit-tested after the move and the pause, right
  before the button goes down.
- **`browser.drag_drop` refuses a CSS end whose element has no box** (it is not rendered, as under `display:
  none`; `drag_end_not_visible`) before any mouse input, with or without an element end. It used to drag from
  or to the top-left corner of the viewport, (0, 0).
- **An upload is no longer a failure to retry once the file has gone in**, by `element_id` or by selector. In
  1.11.0 a page that closed while the upload settled, or any later step that failed with a browser error, made
  it a retryable `browser_action_failed` although the file was in, and a retry uploaded it twice. Now the
  upload is reported as done, with what failed in `target.after_change`. An error that is not retryable,
  raised after the transfer (a policy block, a bot challenge), or a cancelled request, still surfaces, and the
  approval the upload itself required (`REQUIRE_APPROVAL_FOR_UPLOADS`) is spent all the same. By `element_id`
  the action also records `target.delivery`: `{"outcome": "confirmed"}` when the file input holds one file of
  that name and size, now or when its `input` or `change` event fired, else `{"outcome": "not_confirmed",
  "reason": …}`; `verification.verified` follows it (signals `upload_confirmed` / `upload_not_confirmed`). The
  check is best-effort: a page can give the input a file of its own with the same name and size. By selector
  there is no `target.delivery`, and `verification.verified` means only that Playwright's transfer returned.

## [1.11.0] — 2026-10-24

A controller feature release over 1.10.2: a `lean` MCP tool profile and tool packs. The client,
LangChain and MCP bridge packages change only in version; update the controller image to get it.

Upgrade notes: nothing changes for a controller that doesn't set `MCP_TOOL_PACKS` and keeps
`MCP_TOOL_PROFILE` at `curated` or `full`. Those two serve the same tools, in the same order, with
the same descriptors as 1.10.2. A misspelt `MCP_TOOL_PROFILE` still serves `curated`, but now logs a
warning. The error text for a call to a tool the controller doesn't serve has changed (below). Update
anything that matches the old text.

### Added

- **A `lean` MCP tool profile, and tool packs you compose by name.** Every MCP tool now belongs to one
  of 21 packs (`browse`, `sessions`, `tabs`, `downloads`, `devtools`, `harness`, …), and a profile is a
  list of packs. `MCP_TOOL_PROFILE=lean` serves only `browse`: `browser.observe`, `browser.screenshot`,
  `browser.execute_action`, `browser.get_html`, `browser.find_elements`, `browser.request_human_takeover`
  and `browser.close_session`, about 12 KB of `tools/list` against curated's 25 KB.
  `MCP_TOOL_PACKS=tabs,downloads` adds packs to any profile and `-scripting` removes one; an unknown pack
  name stops the controller at startup, with the valid names in the error. `curated` (still the default)
  and `full` list the same tools, in the same order, with the same descriptors as before. The
  `initialize` instructions name only the tools the controller serves, the startup log says what is
  served, and the pack table is in `docs/mcp-clients.md`.

### Changed

- **An unrecognised `MCP_TOOL_PROFILE` now logs a warning.** Any value other than `full` used to mean
  `curated` without a word, `FULL`, `full ` and `leen` included. It still serves `curated`, and the
  startup log now says so and, when one is close, names the closest profile:
  `MCP_TOOL_PROFILE='leen' is not a tool profile (did you mean lean?); serving curated. Use lean, curated or full.`
  Unset, empty or blank still means `curated`, silently.
- **A call to a tool outside the selection names its pack.** It used to say the tool "is in the full MCP
  tool profile"; it now says, for example, `browser.get_console is in the devtools tool pack, which this
  controller does not serve (it serves the curated profile). To use it, add devtools to MCP_TOOL_PACKS or
  set MCP_TOOL_PROFILE=full on the controller.`

## [1.10.2] — 2026-10-24

A security release over 1.10.1, with three advisories: GHSA-3pwm-j9pq-4r6q (PII scrubbing,
traces and fork files), GHSA-m596-6q9j-x6mc (named credentials: artifacts and proxy personas)
and GHSA-hj9r-c8jj-mf9v (mesh peer registration). **Update the controller image.** The client,
LangChain and MCP bridge packages carry one bridge fix; updating them alone does not deploy the
controller fixes.

Upgrade notes:
- **Playwright tracing is off by default, unless your own settings turn it on.** The
  `.env.example` of earlier releases set `ENABLE_TRACING=true`, and an older compose file falls back
  to `true`: if yours do, set `ENABLE_TRACING=false`, then recreate the controller container
  (`docker compose up -d`; `docker compose restart` keeps the old environment) or restart the
  process. A trace is not scrubbed. Earlier releases recorded one for every session, so delete the
  `trace.zip` files under `ARTIFACT_ROOT`, the `fork_*` files under `AUTH_ROOT`, and the built-in
  agent loop's `agent_steps.jsonl` artifacts (which hold the request URLs it saw), with any copies.
  Rotate any credential those files may have held.
- **Network log:** query strings, form bodies and JSON bodies are now stored redacted, so a network
  hook registered on a query value no longer fires, and some ordinary parameter names are redacted by
  their name. `docs/deployment.md` lists both.
- **Named credentials (`API_BEARER_TOKENS`):** proxy personas saved before this release have no owner
  and stay open to every operator until you add `"owner": "<operator id>"` to each entry in
  `PROXY_PERSONA_FILE`; the startup warning names them. Check each persona's server and username
  against your own records first, since another operator could have changed them. Releases 1.8.1 to
  1.9.1 also returned a persona's stored password to any operator: rotate those passwords.
  `/artifacts` no longer serves a directory whose session record is gone, or a file placed directly
  in the artifact root.
- **Mesh (`MESH_ENABLED=true`):** `POST` and `DELETE /mesh/peers` are removed. Manage peers in the
  file `MESH_PEERS_PATH` names, and review it for entries you did not add and for keys that differ
  from what each peer's `/mesh/identity` reports. One invalid entry now turns every peer away: each
  `node_id` must be the SHA-256 of its key, so an entry registered under a name of your choosing,
  which earlier releases accepted, has to be re-registered under its key's hash (`docs/mesh.md`).
- **Credential files are written `0600`.** A backup job or second container that reads them as
  another user must now run as the controller's user.

### Added

- **`PII_SCRUB_SCREENSHOT_STRICT` (default `false`).** With screenshot scrubbing on, a screenshot that
  cannot be scrubbed (no OCR to drive it: tesseract missing or `OCR_ENABLED=false`; or a redaction that
  fails) used to be returned unscrubbed with a warning. In strict mode the image is deleted and the call
  fails with `screenshot_withheld` (HTTP 503); with OCR unavailable, `/readiness` also reports
  `screenshot_scrubbing` as `fail` instead of `warn`. An action that already ran is still reported and
  recorded (action log, audit event, witness receipt), with `observation_error` in place of the image.

### Changed

- **The harness's programmatic verifier tests every `url_matches` pattern in one child process.** It
  started one Python process per pattern (about 180 ms each on Windows). A verification now starts one,
  and another only after a pattern that outlasts the timeout; each stuck pattern still times out on
  its own and the others keep their verdicts.
- **The stealth fingerprint layer is canvas noise only, and says so.** Its WebGL1 `getParameter`
  wrapper returned the real value for every parameter, so it changed nothing except the function's
  `toString()`; it and its unused seed are removed. WebGL values are reported as the browser has them.

### Removed

- **`POST /mesh/peers` and `DELETE /mesh/peers/{node_id}`; mesh peers are managed in the peers
  file.** `/mesh/receive` does not use the bearer token, so registering a peer grants it access
  to the tool gateway, and any API credential could register one (with any key and grants,
  replacing an existing peer's entry too) and keep it after the credential was revoked. Only
  mesh users (`MESH_ENABLED=true`) are affected. **Migrating:** the file `MESH_PEERS_PATH`
  names (default `/data/mesh/peers.json`) already holds every peer registered over HTTP, so
  existing peers keep working. Review it, since anyone with an API token could add entries
  before this release, then add, change and remove peers by editing it; the controller
  re-reads it when it changes. `GET /mesh/peers` and `GET /mesh/identity` stay, and the
  dashboard's mesh list no longer has a Remove button. See the new `docs/mesh.md` for the file
  format. (GHSA-hj9r-c8jj-mf9v)

### Fixed

- **The stdio MCP bridge no longer advertises `resources.subscribe`.** The bridge only POSTs and never
  opens the controller's event stream, so a stdio client that subscribed would never receive
  `notifications/resources/updated`. `initialize` now reports `resources.subscribe: false`, and
  `resources/subscribe` / `resources/unsubscribe` answer JSON-RPC `-32601`. The HTTP transport is
  unchanged. Both bridge copies stay byte-identical.
- **`GET /sessions` still lists closed sessions when Redis is empty or unreachable.** Reading one
  session already fell back to the file record, but the listing asked only Redis, so a Redis restart
  without persistence hid every closed session and a dropped connection turned the listing into an
  error. The listing now merges the file records with Redis (Redis wins for an id in both) and falls
  back to the file records, with a warning, if Redis fails.
- **A cancelled session create no longer leaves its isolated tunnel behind.** Cancelling while
  autossh was starting, or while waiting for the tunnel lock after the startup grace period, left
  the log file open or autossh running and its remote port reserved. Both now stop the process and
  mark the tunnel as errored. A cancelled release still stops autossh, frees the port and closes
  the log before the cancel goes through; it used to be swallowed while release waited for the
  tunnel's heartbeat task.
- **More session routes answer a bad request or a refusal as itself, not as a 500.** `GET
  /sessions/{id}/downloads`, `/tabs`, `/audit`, `/witness`, `/witness/verify` and `/witness/bundle`,
  `POST /sessions/{id}/takeover`, `POST /sessions/{id}/storage-state`, `DELETE /sessions/{id}` and
  `POST /sessions/{id}/fork` now use the same mapping as the other session routes: a refused
  request is 403, an invalid one is 400, and a failure the route does not expect is logged and
  answers 500 "Internal error". A `KeyError` that is not an unknown session still answers 404 but is
  now logged as a warning, so a bug that surfaces as "Not found" can be traced.
- **`STEALTH_PROFILE` is read through settings.** `STEALTH_ENABLED` came from `.env`, but the
  profile was read straight from the process environment, so a run outside Docker with both set in
  `.env` kept the fingerprint profile off. `STEALTH_PROFILE` (`off`, `light` or `aggressive`; any
  other value is rejected at startup) is now a setting, and the compose `environment:` block passes
  it next to `STEALTH_ENABLED`.
- **`docker compose up` no longer stalls on an unhealthy `browser-node` after `cp .env.example .env`.**
  Compose filled `${BROWSER_WS_ENDPOINT_FILE}` in the `browser-node` healthcheck from the project
  `.env`, where `.env.example` set the controller's path (`/data/browser-profile/...`). That path does
  not exist inside `browser-node` (its profile is at `/data/profile`), so the check failed on every
  run and the controller, which waits for a healthy `browser-node`, never started. The healthcheck
  now expands the variable inside the container, and `.env.example` ships the line commented out
  because compose sets the path per service.
- **`CURATOR_ENABLED` no longer sends a placeholder transcript to the model.** With the setting on
  and a provider key present, every closed session started a background review that sent the model
  a fixed placeholder string instead of a transcript, and could write what came back to
  `skills-staging`. Nothing builds a session transcript yet, so the review is not started at all:
  startup logs that the setting is reserved, and `.env.example` says so. The adapter setup and the
  setting itself are unchanged.
- **A screenshot whose redaction fails is reported as failed, not as redacted.** When Pillow could
  not process the image, `scrub_screenshot` returned the original bytes with the matches attached,
  the caller audited `pii_redaction` as `ok`, and the result carried no sign of it. The function now
  raises, the result carries `screenshot_pii_scrub: {"applied": false, "reason": "redaction_failed"}`
  (the field used when OCR is unavailable), and the audit event has status `failed`.
- **A failing disk no longer hangs a download capture.** Reserving a file name for a captured
  download retried forever on any error other than "name already exists" (disk or quota full,
  read-only or mis-owned volume), blocking the event loop. It now tries a generated name once,
  tries at most 20 names, and raises the underlying error, which the background download handler
  logs.

### Security

- **Network inspector: URL-encoded form bodies, JSON string escapes and URL query strings are now
  scrubbed, and a body the scrubber cannot process is withheld instead of stored.** With network
  capture on (the default), `PII_SCRUB_NETWORK` had matched raw text only, so these were stored
  verbatim in the per-session network log (GHSA-3pwm-j9pq-4r6q). Network hooks match the stored
  URL, which is now redacted, so a hook registered on a query value (for example
  `*email=ops%40example.com`) no longer fires; one on the path or a parameter name still does.
  A JSON member whose name marks a secret (`pass`, `db_pass` and `page_token` do; a paging cursor such as
  `next_page_token` does not) is redacted whether or not the body has escapes, with the default
  `PII_SCRUB_REPLACEMENT` a redaction in JSON keeps the body valid (the exceptions: a `Cookie:` line inside a string, a private-key block with no `END`
  line, and a value that is not a string, such as `true` or `12345678`, under a name the password
  pattern knows, such as `"pass": true`), and the fragment of a URL, `Referer` or `Location` is scrubbed like its query. The URL of a
  failed request, which `get_request_failures` and every observation returned as the page asked for it,
  is scrubbed the same way. Playwright traces still record URLs, headers, bodies and typed text as they
  are, and are now off by default (below).
  `docs/deployment.md` lists what the inspector still stores as it is.
- Playwright tracing is now off by default (`ENABLE_TRACING=false` in the settings, `.env.example` and
  the compose file). A session's `trace.zip` holds every request header (`Authorization` and `Cookie`
  included), the request and response bodies, and the text typed into the page, passwords included,
  none of it scrubbed, and every session recorded one, written when tracing stopped or the session
  closed. Set `ENABLE_TRACING=true` to record traces again; traces written before this release stay
  on disk under `ARTIFACT_ROOT` until you delete them (GHSA-3pwm-j9pq-4r6q).
- `fork_session` no longer leaves the source session's cookies on disk. It exported them to
  `AUTH_ROOT/<session_id>/fork_<id>.json` (always plaintext through 1.3.0; from 1.3.1 plaintext unless
  `AUTH_STATE_ENCRYPTION_KEY` is set) for the
  new session to load, and never deleted the file; the file is now removed as soon as the new session
  has loaded it or failed to. Files left by earlier forks stay until you delete them
  (GHSA-3pwm-j9pq-4r6q).
- **`/artifacts` checks the owner of the file a path resolves to.** The mount took the session id
  from the first component of the path as typed, so a path that starts with `..` and comes back
  into the artifact root was judged as a session nobody owns and could be served to another
  operator. The session is now the first component of the resolved file, so every spelling, and
  a link from one session directory into another, is judged by the owner of what is actually
  served. With named credentials (`API_BEARER_TOKENS`), a path whose first component is not a
  session with a stored record (a directory whose record is gone, a file dropped into the
  artifact root) is not served to anyone; deployments without named credentials are unchanged. (GHSA-m596-6q9j-x6mc)
- **Proxy personas belong to the operator who saved them.** With named credentials, any
  operator could list, read the server and username of, overwrite or delete another operator's
  persona, and open a session through it with that operator's stored proxy password. The same
  rule as memory profiles now applies over HTTP and MCP: a persona records the token-verified
  operator who first saved it (an `"owner"` key in `PROXY_PERSONA_FILE`), and to every other
  caller it is missing; overwriting its name answers 409. **Upgrading:** personas that were
  saved before this release have no owner and stay open to every operator, exactly as before
  (shared-token deployments are unaffected). The controller logs a warning at startup when
  named credentials are set and such personas exist, naming how many there are, and a second one
  for any entry whose `"owner"` is an empty string, which counts as no owner. Until an owner is
  added, any operator can overwrite or delete the persona too, not only use it. To make one
  private, add `"owner": "<operator id>"` to its entry in the file. A save by a release before this one
  drops the `"owner"` keys, so do not roll back and save. (GHSA-m596-6q9j-x6mc)
- **Revoking a mesh peer by editing the peers file works reliably.** Deleting the file, or
  saving one that did not parse, left every peer in memory, and the controller rewrote the file
  each time it recorded `last_seen`, which could put a removed peer back or overwrite an edit made
  at the same moment. A missing file is now an empty registry; a file that cannot be read, is not
  valid JSON or has any invalid entry accepts no peer at all (the whole file is refused) and
  logs a `mesh.peers:` warning; `last_seen` is kept in memory and the controller never writes the
  file; and the file is checked on every request, so an edit is seen by the next one. Replace the
  file by rename, and in Docker mount its directory rather than the file; `docs/mesh.md` lists
  the changes that are not noticed. A request already past the sender check is not cancelled.
  (GHSA-hj9r-c8jj-mf9v)
- **The peers file refuses a key its `node_id` does not name.** Any API credential could post a key
  of its own under an existing `node_id`, which kept the id and grants an operator would recognise.
  A `node_id` is the SHA-256 of the peer's 32-byte Ed25519 key, as `/mesh/identity` reports both;
  a file with an entry whose key is not that (or is not strict base64, or is filed under another
  `node_id`) is now refused as a whole and logged. Put back the key the peer's own
  `/mesh/identity` reports. Earlier releases accepted any `node_id` a sender signed as, so an entry
  registered under a name of its own must be re-registered under its key's hash. `add()` checks the
  same. (GHSA-hj9r-c8jj-mf9v)
- **Mesh envelopes with a NaN timestamp are refused.** The timestamp window was written so a NaN
  passed it. A correctly signed envelope from a registered peer carrying one was accepted at any
  time. It now counts as outside the window, for requests and for replies. (GHSA-hj9r-c8jj-mf9v)
- **Files that hold credentials are written owner-only.** The stores wrote every file as
  `0666 & ~umask`, so a `0600` file became `0644` the next time it was saved, and a new
  credentials file was created `0644` under the default umask. `atomic_write_text` now keeps the
  mode a file already has and takes an explicit `mode=`; the proxy persona file, the cron store
  (webhook keys), encrypted auth state, session records, approvals, agent jobs and the MCP
  session store ask for `0600`. A file written by an earlier release is tightened the next time
  it is saved. A backup job or second container that reads these files from the data volume as
  a different user must now run as the controller's user (or `chmod` after each save).

## [1.10.1] — 2026-10-24

A dependency and release-process update over 1.10.0. The client, LangChain and MCP bridge
packages change only in version; the Docker images are rebuilt with the updated dependencies.

### Changed

- Controller dependencies: Pillow 13.0.0 (#208). The GHCR images for 1.10.0 were built before
  that bump, so `latest`, `1.10` and `1.10.1` now carry it.

### Fixed

- **Docker images publish only from green CI on main.** A `v*` tag already had to point at a
  commit on `main` for `docker-publish.yml` to push the `X.Y.Z`, `X.Y` and `latest` image tags.
  It now also needs a successful `CI` push run on `main` for that exact commit, the same gate
  `release.yml` has had since 1.10.0. `edge` and `sha-<commit>` are unchanged: every push to
  `main` still publishes them.

## [1.10.0] — 2026-10-19

A security update and correctness pass over 1.9.1: every finding was reproduced before it was
fixed. The client, LangChain and MCP bridge packages change too: see the client SDK, LangChain
and MCP bridge entries.

**If you verify witness chains, restrict `ALLOWED_HOSTS` or use `workflow_profile=governed`, read
the upgrade notes first.**

Upgrade notes:
- **Witness `valid` now covers the signatures.** `GET /sessions/{id}/witness/verify` and
  `browser.verify_witness` pass `valid` only when the hash walk, the anchor and the signatures
  all pass; the hash walk and anchor alone are now `chain_valid`. A session recorded entirely
  before signing, or by a controller without its signing key, reports `valid: false`.
- **`ALLOWED_HOSTS` is enforced where a page ends up.** A start URL that redirects off the list,
  a switch to a tab on a refused host, a cookie for another domain, and a page that later
  navigates itself off the list are now refused or sent back to `about:blank`.
- **Under `workflow_profile=governed`, `browser.delete_memory_profile` and `browser.cdp_attach`
  ask for approval**, held under `session_id: "controller"`. `browser.cdp_attach` is also
  refused when more than one operator credential is configured, and while live sessions still
  use the current browser.
- **`REQUEST_RATE_LIMIT_EXEMPT_PATHS` entries match one path exactly** (`/artifacts` stays a
  tree). If you exempted a sub-path by listing its parent, list the sub-path itself.
- **`STEALTH_ENABLED=true` now applies its patches**, so sessions with it on present differently
  to sites.
- **REST errors carry their real status.** Routes that answered 500 for an unknown session, a
  timeout or a missing element now answer 404, 403, 400 or the action error's own status, with
  its `code`.
- **If you run `scripts/codex_host_bridge.py` on the host, update it.** An older bridge answers
  400 to a text-only observation, and the new one caps request bodies and read time.
- **The client SDK raises `ValueError` for the ids `""`, `.` and `..`**, and the LangChain
  `AutoBrowserNode` raises `AutoBrowserToolError` when a tool call fails.
- **Removed:** `download_url` from the auth profile export response, and
  `app.browser.dom_pruner`.

### Security

Many of these matter only if you run the controller with named credentials
(`API_BEARER_TOKENS`); each entry says who it affects. The `ALLOWED_HOSTS` entries apply to every
deployment that restricts it.

- **Saved session auth state is owned by the session's operator.** `storage_state_path` is
  relative to the auth root, so another operator could name `<session id>/state.json` and open a
  browser logged in as the session's owner. A session that has an owner now leaves an
  `.owner.json` marker beside the state it saves (including forks), and a path into that
  directory is allowed only to that operator, even after the session closed or the controller
  restarted. State saved by an earlier release has no marker; it falls back to the owner on the
  session record, and stays open if there is none. An unreadable marker refuses.
- **Shadow-browse sessions belong to the session they were made from.** They were created with no
  owner, so any operator could list and drive the copy of another operator's logged-in browser.
  There is still no session-limit check, so `MAX_SESSIONS=1` setups can use shadow browsing.
- **Auth profile export leaves out the plaintext `state.json` when an encrypted
  `state.json.enc` exists.** A plaintext copy saved before the encryption key was set used to
  ride along in the archive. This applies to every deployment. Profiles with only a plaintext
  state still export it.
- **An exported auth archive can be imported only by the operator who exported it.** Export
  writes `<archive>.exporter.json` next to the archive; import refuses anyone else. Archives
  without it (made without named credentials, by an earlier release, or copied in from another
  box) import as before. The importer never becomes the owner: a profile keeps the owner
  recorded in its archive.
- **`browser.cdp_attach` is refused when more than one operator credential is configured.** It
  replaced the one browser every session runs on, so under `API_BEARER_TOKENS` any operator could
  attach a Chrome they control and every session any operator created afterwards ran on it, open
  to their own CDP connection. Run a controller per operator, or set `CDP_CONNECT_URL` at startup.
  With a single credential (or none) nothing changes except that attach is now refused while live
  sessions still use the current browser (close them first), and the browser it replaces is
  disconnected instead of leaking.
- **Closing a session no longer closes the shared browser after a swap.** The check compared the
  session's browser with `manager.browser`, which `cdp_attach` replaces; sessions now record whether
  their browser is their own (isolated runtime, shadow-browse window) and only those are closed.
- **Harness runs and staged skill candidates belong to the operator who started them.** Any
  operator could list every run, read its trace (traces of live sessions carry page content),
  graduate someone else's candidate, and read or drift-check their staged skills. A run now
  records the verified operator, and its candidate inherits that owner, which is signed into the
  envelope so stripping it is caught. Other operators get a permission error and don't see these
  in lists. An owned candidate's id now also depends on its owner, so two operators staging the
  same contract no longer overwrite each other. Runs and candidates from earlier releases, or
  made without named credentials, stay open.
- **An unowned cron job fires as nobody.** Triggering one by hand or by webhook ran it as the
  triggering operator, and the session and agent job it created became theirs. It now fires as
  the anonymous identity, the same as a scheduled fire. Owned jobs still fire as their owner.
- **Memory profiles belong to the operator who first saved them.** Another operator could list,
  read, append to and delete a profile, and load it into their own session. Profile text goes
  into the agent's goal prompt, so that was a prompt-injection path between operators. Those
  calls are now refused for another operator's profile, which is also left out of lists, and the
  name-collision error no longer reveals other operators' profile names. Profiles saved without
  named credentials, or before this release, stay open, and appending to one doesn't claim it.
- **MCP `resources/list` names only sessions you may use.** It listed every operator's session ids
  and their screenshot, dom, console and network URIs; `resources/subscribe` accepted them, and the
  update stream notified the subscriber whenever those sessions were active. Listing, subscribing
  and notifications now cover only sessions you may use. Unowned sessions are still listed for
  everyone, and reading another operator's session content was already refused.
- **Uploads can no longer be taken from another operator's session.** A session's staged files
  live in `UPLOAD_ROOT/<session id>/`, and the upload path check also allowed the shared
  `UPLOAD_ROOT`, so another operator could upload `<session id>/file` (or the absolute path) into a
  page of their own. A file in a session's directory can now be uploaded only by the operator who
  owns that session, also after it closed; files placed directly in `UPLOAD_ROOT` stay shared.
  Deployments without named credentials have no owners and see no change.
- **`GET /proxy-personas/{name}` no longer returns the proxy password.** It returned the stored
  persona, plaintext password included, to any caller with an API token; it now answers the same
  shape as the list (`has_password`). Session creation with `proxy_persona` is unchanged. This
  applies to every deployment.
- **Session creation checks where the start URL ended up.** It checked the URL it was given but
  not the page it landed on, so an allowed start URL that redirected to a host outside
  `ALLOWED_HOSTS` left a live session there. It now refuses and rolls the session back.
- **Switching to a tab on a disallowed host is refused.** `activate_tab` made any open tab the
  session's page, including a popup a page had opened onto a host the list refuses. It now
  refuses before the active page changes (REST: 403).
- **A blocked action that can't be rolled back no longer leaves the session, or its error, on the
  disallowed page.** After `location.replace()`, or in a popup's first page, there is no history
  entry, so going back did nothing: the session stayed on the refused page and the 403 carried
  its text and a screenshot. The session now moves to `about:blank`, and the error reports only
  the URL.
- **`browser.set_cookies` is held to `ALLOWED_HOSTS`.** It added cookies for any domain, so a
  caller confined to one site could plant a cookie on another. Each cookie's `url` or `domain` is
  checked first, and one disallowed cookie refuses the whole call.
- **`ALLOWED_HOSTS` holds after an action, not only at it.** A page that navigated itself later
  (a timer, a late redirect, a popup's own script) stayed on the disallowed host, and `observe`
  returned its title and text. Every page the session owns now has a listener that sends a
  top-level page that lands off the list back to `about:blank` and records a `navigation_blocked`
  audit event. Until it has, observe, screenshots, action snapshots, the page-reading MCP tools
  and the DOM resource refuse (REST: 403), actions other than `navigate` and `go_back` are
  refused, and summaries leave the page title empty. The list governs the top-level page only:
  iframes and subresources from other hosts are not filtered, and raw CDP passthrough is
  unchanged.
- **Witness verification no longer accepts a chain with every signature stripped.**
  `GET /sessions/{id}/witness/verify` and `browser.verify_witness` rejected an unsigned receipt
  after a signed one, but a chain with no signed receipt at all reported `signatures.valid: true`.
  Anyone who could write the witness directory could edit receipts, drop every signature,
  recompute the hashes and the anchor, and pass. Such a chain now fails with the reason
  `scripts/verify_witness_bundle.py` already gave. Unsigned receipts before the first signed one
  still pass. The top-level `valid` now passes only when the hash walk, the anchor and the
  signatures all do, so a client that reads that one field can't accept a stripped chain. The
  hash walk and anchor result alone moved to `chain_valid`. A session recorded entirely before
  signing existed, or by a controller that couldn't load its signing key, now reports
  `valid: false` with `chain_valid: true`, which is accurate. The new section in
  `docs/deployment.md` says what a signature can prove while the key sits beside the receipts,
  and how to pin a head hash for evidence that survives a compromised host.
- **Governed tools that name no session ask for approval.** `browser.delete_memory_profile` and
  `browser.cdp_attach` ran unapproved even under `workflow_profile=governed`, because the approval
  needed a session to belong to. They now return `approval_required` like every other governed
  tool, and the approval is held in a controller-wide scope (`session_id: "controller"`). With
  named credentials it belongs to the operator who asked: another operator cannot list, decide,
  reuse or spend it. Approvals now record that operator in a new `owner` field; records written
  before it load as unowned. Agents that use `governed` see the new approval step on those tools.
- **PII scrubbing removes a whole private key.** The `pem_header` pattern replaced only the
  `-----BEGIN ... PRIVATE KEY-----` line and left the key itself and the END line in scrubbed
  text, network bodies and console messages. It now removes the block from BEGIN to the matching
  END for every private-key label (PKCS#8, RSA, EC, DSA, OpenSSH, encrypted, PGP), with LF or CRLF
  line ends, and everything after a BEGIN that has no END. Public keys are no longer touched:
  they are not secret, and redacting their header only corrupted them. The pattern keeps its name,
  so `PII_SCRUB_PATTERNS` lists that name it still work.
- **Request bodies without a text content type are scrubbed.** A `fetch` with a string body (no
  content type), `application/yaml` and unrecognized vendor types went into the network inspector
  verbatim. A body is now scrubbed whenever it is text: its type names text, or it has no
  recognized type and decodes as UTF-8. Images, audio, video, fonts, common binary types and
  non-UTF-8 bytes are left as they were. An `application/html` response is now scrubbed too.
- **Screenshot scrubbing without tesseract is no longer silent.** Screenshot redaction draws over
  OCR-detected words, and tesseract ships only in the Docker image, so outside Docker every
  screenshot went out unredacted with no sign of it. `GET /readiness` now has a
  `screenshot_scrubbing` check that warns when tesseract is missing or `OCR_ENABLED=false`, the
  controller logs one warning, and observe, screenshot and action-snapshot results carry
  `screenshot_pii_scrub: {"applied": false, "reason": ...}`. The new section in
  `docs/deployment.md` lists what scrubbing covers and what it does not: page content, raw CDP
  (which can read back a typed password) and the console messages inside an observation.
- **Closed sessions keep their owner when Redis loses the record.** With `REDIS_URL` set, the
  session store read only Redis, so an evicted key, a Redis restart without persistence or a
  dropped connection made a closed session look unowned, and another operator's ownership check
  let them in. It now falls back to the file copy, which is written first, and logs a Redis error
  when there is one.
- **The noVNC page can no longer be framed.** The WebSocket origin guard stops a page from
  opening its own connection to the desktop, but a page the in-container browser visited could
  still embed `http://127.0.0.1:6080/vnc.html`, whose own WebSocket passes the guard. Every
  response noVNC serves now sends `X-Frame-Options: DENY` and
  `Content-Security-Policy: frame-ancestors 'none'`. `docs/deployment.md` now says what the guard
  and the headers each prevent. This applies to every deployment that exposes noVNC.
- **Client SDK ids can no longer retarget a request.** The client put ids into URL paths as
  given, so `get_session("../approvals/x/approve")` reached another route. Every id in a path,
  the event stream included, is now percent-encoded, and `""`, `.` and `..` raise `ValueError`.
  The client also gains `close()` and `aclose()` for an instance used outside `with`.
- **The Codex host bridge caps request bodies and times out stalled reads.** If you run
  `scripts/codex_host_bridge.py`: a body over 32 MiB gets 413 without being read, a bad
  `Content-Length` gets 400, and a client that stalls for 30 s gets 408. Before, a 64 MB body was
  read and parsed, and one idle connection held a handler thread for good.

### Fixed

- **The Playwright export script replays the session that ran.** `GET
  /sessions/{id}/export-script` and `browser.export_script` produced `page.goto('')` and
  `page.locator('').first.click()`, in reverse order, starting at the page the session ended on:
  anyone who used the export got an unusable script. It now replays the steps oldest first from
  the session's own start URL, and says when the session had more than 5000 audit events
  (`"truncated": true`). Steps that targeted an element by `element_id` carry a comment, because
  that id exists only after `observe()` tags the live page. A start URL containing `"""` or a
  backslash escape such as `\N{...}` could stop the script compiling; it is now a one-line comment.
- **A closed session's replay script and trace are served.** `GET /sessions/{id}/export-script`,
  `GET /sessions/{id}/trace` and `browser.export_script` answered 404 once a session closed, which
  is when `trace.zip` exists. They now read the stored session record; another operator's session
  and an unknown id still answer 404.
- **`STEALTH_ENABLED=true` runs the stealth patches.** The init script was a bare arrow function,
  which `add_init_script` defined and never called, so no patch applied: operators who turned
  stealth on got none of it, and sessions with it on now present differently to sites. It also
  adds `window.chrome.runtime` when headless Chromium already defines `window.chrome` without it.
- **Stealth and `ACTION_TIMEOUT_MS` apply to every tab and popup.** Both were set on a session's
  first page only, so tabs opened later and popups ran unpatched and waited Playwright's 30 s
  default. They are now set on the browser context, in session creation and in shadow browse.
- **The `STEALTH_PROFILE` canvas noise is applied once per read.** The fingerprint script wrapped
  `getImageData` again on every `getContext('2d')` call, which returns the same context, so each
  call stacked another layer of noise, and enough calls overflowed the page's stack.
- **`toDataURL` leaves the canvas as it found it.** With both stealth scripts on, each export read
  a pixel through the noisy `getImageData` and wrote it back, so the canvas drifted on every call.
  The base script now reads it with the native method.
- **REST errors keep their real status and `code`.** On the session action routes, observe,
  screenshot compare, `/approvals/{id}/execute` and the auth-profile export and import, a
  catch-all ran before the app's error handlers: an unknown or closed session, a timeout or a
  missing element came back as 500 "Internal error", which hid the `code` agents act on. They now
  answer as themselves: 404 with the session message and `code` (`unknown_session`,
  `session_closed`), a browser action error's own status and payload, 403 for a refusal, 400 for
  a bad value and 404 for a missing file. Anything unexpected is still logged and answers 500.
  `observe` and `wait` on an unknown session now carry that message and `code` instead of a bare
  "Unknown session".
- **Agent steps work on a text-only observation.** `observe(preset="text")` returns no
  screenshot, and `PERCEPTION_PRESET_DEFAULT=text` makes that the default, but the Claude, Gemini,
  OpenAI and OpenAI-compatible adapters (and the Codex CLI and host-bridge paths) read the
  screenshot unconditionally and crashed with `KeyError` or `TypeError` before sending anything.
  They now send a text-only request. If you run `scripts/codex_host_bridge.py` on the host,
  update it too: an older bridge answers 400 to a request without a screenshot.
- **The LangChain integration raises a clear error when a tool call fails.** `AutoBrowserNode`
  raised `JSONDecodeError` or `KeyError`, or returned an empty state, when the controller reported
  `isError: true`. It now raises `AutoBrowserToolError` with the tool name, the error text, and
  `code` and `retryable` when the controller sent them, so a graph can tell a missing session from
  a bad selector. Building `AutoBrowserTool` without `langchain-core` now raises an `ImportError`
  naming `pip install "auto-browser-langchain[langchain]"` instead of "takes no arguments".
- **MCP tool results that are lists no longer break strict clients.** Over the streamable MCP
  transport, a tool whose result is a list or a scalar (`browser.list_sessions`, for one) was
  sent with that value as `structuredContent`, which the MCP spec requires to be an object, so
  clients that validate the response rejected it. Such results now carry only the text block,
  which already held the same JSON. Thanks to @sean0407 (#192).
- **Network capture no longer buffers downloads.** With body capture on, the inspector checks
  the content type and the declared length before reading a response, so a large binary or
  oversize body is no longer loaded into memory only to be dropped. A response with no declared
  length is still read and then size-checked.
- **A failed session create no longer leaves its network log behind.** When a create failed
  after the session hook ran (an unreachable start URL, for one), the dead session's network
  inspector and CDP entry stayed, readable through `/sessions/<id>/network/requests`. They are
  now removed.
- **Isolated-session profiles, cookies included, are deleted on every exit path:** when Docker
  has already lost the container, at startup for anything a crashed controller left (except the
  profile of a container that could not be removed), and when a container fails to start.
  `ISOLATED_BROWSER_KEEP_CONTAINERS` still keeps everything.
- **The Universal Verifier process stops when the caller gives up.** A cancelled verification
  or a shutdown now kills and reaps the child instead of leaving it running.
- **The cleanup sweep no longer freezes the controller.** The file walk runs in a worker
  thread, so sessions and requests keep responding during a sweep (it blocked for 13 s on 6,000
  files).
- **Session tunnels start in parallel.** Concurrent provisions no longer wait out each other's
  startup grace period (four took 4 s, now about 1 s). A tunnel released while it is starting
  fails that provision cleanly, and a failed metadata write no longer leaks a log handle.
- **A pathological `url_matches` pattern can no longer hang the harness.** Python's regex
  engine can't be interrupted, so matches now run in a short-lived child process with a 2 s
  limit. A postcondition that times out is not satisfied, a forbidden state that times out
  counts as hit, and both are listed in the result's `details.regex_timeouts`.
- **A bad Host header is refused first.** It now gets the host check's 400 before the bearer
  token, operator id and rate limit are looked at, instead of a 401 or the missing-operator 400,
  and it no longer spends rate-limit budget. Every other answer is unchanged.
- **`/healthz/deep` is rate-limited.** An entry in `REQUEST_RATE_LIMIT_EXEMPT_PATHS` exempted
  everything under it, so the deep health probe, which opens a browser context and renders a page
  on every call, was unlimited for a token holder. Entries now match one path exactly;
  `/artifacts` stays exempt as a tree, because the UI loads many files from it. If you exempted
  a sub-path by listing its parent, list the sub-path itself. `/docs/oauth2-redirect` is now
  counted.
- **An open share link no longer slows the session it shows.** The share page polls every 5 s,
  and each poll ran a full observation (DOM, accessibility tree, OCR) under the session lock and
  kept the screenshot for good, so the agent waited on the viewer and the artifact directory
  filled with PNGs. A poll now takes one redacted frame plus the URL and title, and frames older
  than a minute are deleted.
- **Generated `SKILL.md` front matter is always valid YAML.** The goal went into `description`
  unquoted, so `Open the page: log in` made the file unparseable and a goal with a line break
  could add keys of its own, such as `status` or `safety_class`, to a file that is then signed.
  It is now a quoted string that reads back exactly.
- **Releases publish only from green CI on main.** A `v*` tag already had to point at a commit on
  `main`. It now also needs a successful `CI` push run on `main` for that exact commit, and the
  release workflow's actions are pinned to commit SHAs. For maintainers: tag after CI on main
  has finished. A tag pushed while CI is still running fails, and re-running the release
  workflow once CI is green publishes it.
- **The reverse-SSH sidecar writes `reverse-ssh.json` atomically and escapes it.** A host, user
  or URL containing a quote or a backslash produced invalid JSON, and the controller could read
  a half-written file. A non-numeric port or interval now stops the sidecar at start instead.
- **The smoke scripts send the bearer token** when `API_BEARER_TOKEN` is set, so `make smoke-*`
  works against a controller that requires it.
- **Docs:** the login example reads the session's `id` (it read `session_id`, which the response
  doesn't have), `docs/llm-adapters.md` matches the current providers (OpenAI's Responses API,
  Claude's `tool_choice: auto`, async calls, the isolation modes), `ROADMAP.md` names the current
  release, and `SECURITY.md` lists all five published advisories.
- **The dashboard says when a request failed.** Saving an auth profile, starting or reopening
  a login session, approving or rejecting, cancelling, resuming or discarding an agent job,
  removing a peer, and loading any table showed "Saved" or an empty list when the controller had
  answered with an error or could not be reached. Each now shows the controller's own error
  message, and one failing table no longer blanks the others.
- **Audit views show who did what.** The dashboard's audit table and the session replay page read
  `operator_id` and `data`, which no audit event has, so those columns were always blank. They now
  show the event's operator id, and the replay page shows its details as JSON.
- **The MCP bridge reports a failed re-initialize.** When the controller restarted and the
  bridge's `notifications/initialized` call then failed, the bridge kept the half-open session
  and the next call failed with "Session not initialized". It now drops that session, relays the
  controller's original error, and retries on the next call.
- **Named tokens count as authentication.** `GET /readiness`, the runtime-policy warning and the
  remote-access info said the API was unauthenticated when only `API_BEARER_TOKENS` was set.
- **`/version` answers under `REQUIRE_OPERATOR_ID`, and `/ui/*` redirects.** A version probe no
  longer needs an operator id, and an old bookmark such as `/ui/sessions` redirects to the
  dashboard as `/ui` does, instead of answering 401.
- **A network hook whose pattern contains `/` can be removed.** `DELETE
  /sessions/{id}/network/hooks/{pattern}` never matched a pattern such as `*/api/*`. A Host header
  such as `[::1]evil.com` is also no longer read as loopback.

### Changed

- Controller dependencies: FastAPI `>=0.143.2,<0.144` (#177, #183), Starlette 1.8.0 and
  pydantic-settings 2.16.0 (#191); ruff 0.17.1 for development.

### Added

- **`FORCE_GOVERNED_APPROVALS` makes governed approvals the server's rule.** Off by default,
  because it stalls an existing agent loop on its first write. When on, every MCP tool that writes
  (the 16 with a governed kind) and every step of the agent loop (MCP-queued steps and runs, REST
  `/agent/*`, cron) needs an operator's approval, whatever `workflow_profile` the request carried.
  `GET /readiness` warns on a deployment with several operator credentials that leaves it off. It
  does not change the REST action routes (`/sessions/{id}/actions/*`), which a bearer-token holder
  can still call directly: it governs agents that act through MCP tools and the agent loop, not
  token holders. `AUTONOMOUS_APPROVALS=true` lets agents approve their own calls and cancels it.

### Removed

- `download_url` from the auth profile export response. It pointed at `/auth-export/<archive>`,
  which no route serves; the archive is the file named by `archive_path` in the auth root.
- `app.browser.dom_pruner` (`DOMPruner`). Nothing in the controller called it, and it read keys
  that real observations don't have, so it pruned nothing. Only code importing it directly is
  affected.

## [1.9.1] — 2026-10-05

Prebuilt images: you can now run auto-browser without building it. Nothing else changes for
users; the client, LangChain and MCP bridge packages are unchanged apart from their version.

### Added

- **Prebuilt Docker images on GHCR** (thanks @FRFlo, #151):
  `ghcr.io/lvcidpsyche/auto-browser-controller` and `ghcr.io/lvcidpsyche/auto-browser-browser-node`.
  Run them with `docker compose -f docker-compose.yml -f docker-compose.images.yml up -d`;
  `AUTO_BROWSER_IMAGE_TAG` picks `latest` (default), a release such as `1.9.1`, or `edge`.
  Every push to `main` publishes `edge` and `sha-<commit>`. Release tags publish `X.Y.Z`, `X.Y` and
  `latest`, and only from a tag on `main` whose version matches the packages. Each image carries an
  SBOM and a build-provenance attestation:
  `gh attestation verify oci://ghcr.io/lvcidpsyche/auto-browser-controller:1.9.1 -R LvcidPsyche/auto-browser`.
  The images are linux/amd64. The controller image is built without the agent CLIs, so
  `*_AUTH_MODE=cli` still needs a local build with `INSTALL_AGENT_CLIS=true`.

### Changed

- Controller dependencies: FastAPI `>=0.142.2,<0.143` (#173); ruff 0.16.10 and cryptography
  50.0.2 (#172).

## [1.9.0] — 2026-10-02

A security and correctness pass over 1.8.1, from a maintainer review: every finding was
reproduced before it was fixed. The client, LangChain and MCP bridge packages are unchanged
apart from their version.

**If you run the controller with named credentials (`API_BEARER_TOKENS`) or keep settings in
`.env`, read the upgrade notes first:** operators no longer share sessions, and every setting
in `.env` now takes effect.

Upgrade notes:
- **Everything in `.env` now reaches the controller.** Docker Compose used to pass only the
  keys it listed, so `API_BEARER_TOKENS`, `PII_SCRUB_*`, `WITNESS_*`, `SHARE_TOKEN_SECRET`,
  `APPROVAL_WEBHOOK_*` and other documented settings set in `.env` were ignored. Check your
  `.env` for values you did not mean to apply. Needs Docker Compose 2.24 or newer.
- `SHADOW_BROWSE_ENABLED` and the new `CURATOR_ENABLED` default to `false`. The curator
  reviewed every closed session with a paid model call whenever a provider key was set.
- `scripts/verify_witness_bundle.py` fails a bundle with no signed receipt unless you pass
  `--allow-unsigned`, and takes `--expect-key-id` and `--expect-head` to pin the signing key
  and the chain head.
- Staged skill candidates signed before this release fail verification, because their
  envelopes carry no file hashes; induce them again.
- A `type` action whose field will not keep keyboard focus now fails with `focus_lost`
  instead of typing elsewhere.
- `browser.approve_approval` over MCP (the `full` tool profile) refuses unless
  `AUTONOMOUS_APPROVALS=true`. It let the agent whose action was waiting approve it. Operators
  approve in the dashboard or with `POST /approvals/{id}/approve`, as before.
- **With named credentials (`API_BEARER_TOKENS`), sessions belong to the operator who created
  them**, as auth profiles already did. Other operators no longer see or use them. Sessions
  created before the upgrade, and every session under the shared `API_BEARER_TOKEN`, stay
  unowned and open to all.
- `OPENAI_MODEL` defaults to `gpt-6.1-sol` (was `gpt-5-mini`). Set it to keep another model.

### Security

- Hardened approvals, navigation, the TOTP autofill, click and type targeting, PII scrubbing,
  auth-state handling and witness bundle verification. Advisories will follow with details.
- **Sessions are no longer shared between operators** under named credentials. Operator A could
  drive operator B's logged-in session, read its pages and save its cookies to a profile of
  their own, which got around auth profile ownership. Another operator's session is now left
  out of listings and answers like one that does not exist: over REST, MCP and `/artifacts`,
  with its approvals, agent jobs and cron jobs. Agent jobs run as the operator who queued them,
  and cron jobs fire as their creator and may use only the auth profiles their creator can.
  The audit log stays operator-wide.

### Fixed

- **Governed approvals work.** A governed payment, post, account change, destructive action
  or upload through the MCP gateway failed with "already being executed" and never ran. A
  governed write approval is consumed when its action runs, through the orchestrator and
  `POST /approvals/{id}/execute` alike, and an expired approval is refused before its action
  runs.
- **A page that never answers cannot hang the controller.** A page running a script in an
  endless loop hung `GET /sessions` and `DELETE /sessions/{id}`; with the default
  `MAX_SESSIONS=1` it held the only slot until a restart. Listing no longer fails when a
  session closes mid-list.
- **The Claude provider works on Claude Opus 5.5, Sonnet 5.5 and Fable 5.1**, which reject a
  forced tool choice, and has room for their thinking (16k output tokens instead of 1,024).
- **The OpenAI provider works again.** OpenAI rejected every step: the request carried a schema
  strict mode refuses and a `temperature` current models reject, and current models refuse
  function tools on chat completions while they reason. It now uses the Responses API. The
  OpenAI-compatible providers stay on chat completions and now send neither.
- **PII scrubbing:** card numbers written in groups, provider API tokens, Basic credentials,
  cookie headers and OAuth codes are redacted; screenshots are redacted a line at a time, so
  phone numbers and grouped card numbers no longer slip through; console text that a page
  writes can no longer stall the controller.
- **Codespaces start**, with an API token generated into `.env`. `make doctor` sends the token
  and refuses to run when it cannot see whether live sessions would be interrupted.
- **Isolated sessions work from any checkout directory**: the browser image has a fixed name.
- **Releases publish only commits on `main`.**
- **The browser node waits for its display** before starting the takeover desktop, which
  could otherwise fail silently while the container reported healthy.
- **Memory profiles** whose names map to the same file (`github.com`, `github_com`) are no
  longer merged.
- `read_download` reads CSV files on Windows hosts, where the registry types them as Excel.

### Changed

- The test suite writes only to a temporary directory; it used to write into `./data` (and
  under `make test`, into the real witness chains). Real-Chromium tests run in CI.

### Added

- `PII_SCRUB_ACTION_SCREENSHOTS` (default `true`). Redacting the before and after snapshots of
  every action runs OCR twice per action; `false` skips it for those snapshots, which makes
  actions much faster, while observation screenshots stay redacted.
- The dashboard's Operator column shows who owns each session; it was always empty.
- **Autonomous mode**, off by default: with `AUTONOMOUS_APPROVALS=true` agents approve their
  own actions instead of waiting for an operator. The built-in agent approves and carries on,
  and MCP clients may call `browser.approve_approval`. Every approval is still created and
  recorded, marked `decided_via: agent` on the approval, in the audit log and in the witness
  chain. An agent cannot overturn an operator's decision, or approve again an action an
  operator rejected in that session; those wait for an operator.

### Removed

- **`/workflows`** (`POST /workflows/run`, `GET /workflows/runs`, `GET /workflows/runs/{id}`)
  and the dashboard's Workflow Runs panel. The engine had no step actions registered, so every
  run failed; it will come back when there are workflows to run with it. `WORKFLOWS_ROOT` is no
  longer read, and run records already in `/data/workflows` can be deleted. A mesh peer asking
  for a `workflow:` capability now gets "Unknown capability kind" instead of "not yet wired".

## [1.8.1] — 2026-09-26

A reliability, speed, polish and security pass over 1.8.0.

**If you use the stdio MCP bridge (`uvx auto-browser-mcp`, Claude Desktop, Cursor), upgrade.**
It now survives a controller restart, reports auth and rate-limit errors instead of hanging,
and can send an operator id (`--operator-id` / `AUTO_BROWSER_OPERATOR_ID`) to controllers
that set `REQUIRE_OPERATOR_ID=true`.

Upgrade notes:
- `browser.create_session` with `totp_secret` now needs `totp_hosts` unless it also sets
  `start_url`, whose host becomes the default.
- noVNC takeover now accepts only its own page on a loopback host name. If your takeover URL
  uses another name (a LAN IP, a bastion, a domain), set `NOVNC_ALLOWED_HOSTS`.
- `POST /workflows/run` validates its steps up front. Unknown keys, duplicate ids, unknown
  dependencies and unbounded retries now get a 422 before the run starts.
- `GET /sessions/{id}/trace` returns a `viewer_url` that opens the Playwright trace viewer
  without a trace preloaded, plus a new `viewer_command`. The old `?trace=` link never loaded.
- `GET /audit/events` with `limit <= 0` returns an empty list.
- The controller image no longer includes the test tooling. Build with `INSTALL_DEV_DEPS=true`
  to run pytest inside it (`make test` and CI already do).

### Security

- Hardened session creation, the TOTP autofill, approvals, the noVNC takeover
  socket and witness receipts. Advisories will follow with details.
- **New:** `totp_hosts` on `browser.create_session` names the hosts where
  one-time codes may be typed; it defaults to the `start_url` host, and a
  `totp_secret` with neither is now refused.
- **New:** noVNC takeover accepts only its own page on a loopback host name.
  If your takeover URL uses another name (a LAN IP, a bastion, a domain), set
  `NOVNC_ALLOWED_HOSTS`; if a proxy in front of the noVNC port rewrites the
  `Host` header, set `NOVNC_ALLOWED_ORIGINS`. Isolated session containers get
  `ISOLATED_TAKEOVER_HOST` automatically.
- An approval for text typed with `sensitive: true` can be executed only by the
  controller process that created it (not after a restart, by another uvicorn
  worker or replica, or after 24 hours); the action then asks for a new one.

### Fixed

- **Actions that ran are no longer reported as failed.** A page that navigated just after a
  successful action (a click whose handler redirects) used to turn the result into a failure,
  so callers retried and submitted forms twice. The result now comes back with an
  `observation_error` telling the caller to observe again. When a failed page can't be
  snapshotted, the normalized error is kept.
- **An approved action runs at most once**, even when two requests carry the same approval.
- **MAX_SESSIONS counts sessions still being created**, so concurrent creates can't exceed it.
- **Sessions tear down cleanly.** A failing teardown step no longer strands the session and its
  slot. A cancelled create rolls back the way a failed one does.
- **Tabs:** a session whose active tab closes itself moves to another tab. A tab that fails to
  open is closed. The CDP routes and the network inspector follow every tab, not only the first.
- **Stores:** every atomic JSON write gets its own temp file. Before, 16 parallel upserts lost
  177 of 320 writes. An audit trim no longer drops events appended while it runs. Damaged
  auth-profile metadata refuses access instead of reading as unowned. Encrypted storage state
  never leaves a plaintext temp file behind.
- **MCP:** sessions are evicted by last use, not creation time. The stdio bridge re-initializes
  when the controller forgets its session, and answers HTTP-layer errors (401, 400, 429) as
  JSON-RPC errors.
- **Cron:** jobs no longer leak sessions or silently stop firing. An invalid schedule is
  refused, and a webhook and a scheduled fire can't both start a run.
- **Agent jobs:** jobs caught mid-cancellation by a restart are settled. Resumed jobs stay
  within the context-hint limit. The orchestrator ends a run with its history when the page
  can't be observed, instead of returning a 500.
- **Providers:** rate-limited retries honor `Retry-After`, and a cancelled job kills its CLI
  subprocess.
- **Also:** session event streams end when the session closes. The network inspector bounds
  in-flight requests. A cancelled tunnel provision stops its autossh. The cleanup sweep no
  longer removes a new session's empty directories. Two downloads with the same name keep both
  files.

### Changed

- **Actions and observations are faster (#161).** A selector click on a
  300-row page went from about 1,440 ms to 1,250 ms against local Chromium.
  An observation's page summary now makes two concurrent browser calls instead
  of four in a row. The screenshot is taken while the DOM is read. The
  post-action bot-challenge probe makes one call instead of three. Tab titles
  are fetched together. The accessibility outline no longer builds nodes past
  its 30-node cap, which halves its parse time on large pages (85 ms to 47 ms
  on a 3,000-row page). Observation payloads keep the same shape.
- **Human-like mouse moves keep their intended 4–18 ms pacing (#161).** Each
  step used to sleep its full gap on top of the ~17 ms Chromium takes to
  dispatch a move. Moves now arrive at about the browser's dispatch rate, and
  the path keeps its shape and step count.
- **Listing audit events stops at the limit (#161).** The file-backed store
  read and validated every stored event (up to 10,000) to return the newest
  100. It now reads newest first and stops once it has enough: 67 ms to 6 ms.
  A `limit` of zero or less now returns nothing.
- **The controller image leaves out the test tooling (#161).** pytest, ruff,
  pip-audit and the rest of `requirements-dev.txt` (about 58 MB) install only
  when the image is built with `INSTALL_DEV_DEPS=true`, which `make test` and
  CI set. Anything else that runs pytest inside the image needs that build arg.
- **Clearer MCP tools.** Every argument of the curated tools is described. An
  unknown tool name points at the tool the caller meant. Validation errors drop
  pydantic's "Value error" prefix.
- **The OpenAPI docs are grouped by area** instead of one flat list.

### Added

- `auto-browser-client`: `operator_id` and `headers` arguments, and `AutoBrowserError` exported
  from the package.
- `auto-browser-langchain`: `operator_id` on `AutoBrowserTool`, `list_tools` and
  `AutoBrowserNode`. The tool description no longer names tools that don't exist.
- Stdio bridge: `--operator-id` / `AUTO_BROWSER_OPERATOR_ID`, and `--help` names each flag's
  environment variable.

### Documentation

- The README's production settings now let the controller start. They were missing `SHARE_TOKEN_SECRET` and
  `CONTROLLER_ALLOWED_HOSTS`, and a test runs the startup policy over them.
- `.env.example` lists every setting and the current default models, and a test keeps it that
  way.
- Doc examples that called removed tools are fixed, and a test validates every curl MCP call in
  the docs.
- The README says when to choose per-session isolation, and the architecture doc marks the
  shipped roadmap phases.

## [1.8.0] — 2026-09-25

Two passes since 1.7.0.

The first was a security scan-and-review of the controller, client and
deployment files. The automated scanners (bandit, semgrep, pip-audit, npm
audit, shellcheck) turned up nothing that survived triage. Everything under
*Security* came from reading the code and was confirmed with a reproduction
before it was fixed.

The second drove the controller the way an agent does, over MCP against a real
Chromium. It measured what each call costs a model and fixed what got in the
way. Sizes below are measured, and each fix was checked live.

**If you run the controller directly without a token and reach it by a
non-loopback name, read the Host-header entry before upgrading** — that
configuration now answers `400` until you list the name in
`CONTROLLER_ALLOWED_HOSTS`.

**If you integrate over MCP or use CLI-authenticated providers, read
*Changed*.** Four defaults changed:
- MCP results are compact (`detail="full"` returns the old payload).
- The default tool profile lists 20 tools (`MCP_TOOL_PROFILE=full` for the
  rest).
- `browser.get_html` returns at most 20,000 characters per call (page with
  `offset`).
- The controller image leaves out the provider CLIs unless built with
  `INSTALL_AGENT_CLIS=true`.

Two smaller upgrade notes:
- `./data/browser-profile` and `./data/downloads` become owned by uid 10001 on
  the host, because the browser no longer runs as root.
- `/share/{token}/observe` returns only what the share viewer renders.

### Security

- **The navigation allowlist judged a different host than the browser loaded.**
  `ALLOWED_HOSTS` checks parsed URLs with Python's urllib, but Chromium parses
  them per the WHATWG URL standard, and for http(s) the two disagree on
  backslashes. For `http://evil.com\@example.com/` urllib reports
  `example.com` (everything before the last `@` is userinfo to it) while
  Chromium loads `evil.com` with path `/@example.com/`. Appending
  `\@<allowlisted-host>` to any URL got it past the allowlist on navigate,
  `create_session`'s `start_url` and tab open — cloud metadata addresses
  included. Checks now parse the URL in the form both parsers agree on
  (`app/url_safety.py`). A differential fuzz of 31,174 generated URLs against
  Chromium's own `new URL()` found 28 such bypasses before the change and none
  after.

- **Opening a tab skipped the navigation allowlist entirely.**
  `POST /sessions/{id}/tabs/open` called `page.goto()` directly, so it reached
  any http(s) host that `navigate` refuses. The URL is now checked before a page
  is created, and a refusal is a `403`.

- **A tokenless controller accepted any Host header — DNS rebinding.** A
  controller without a bearer credential is permitted only on loopback, but
  nothing checked the Host header unless `CONTROLLER_ALLOWED_HOSTS` was set.
  Compose sets it; running the controller directly left it empty, and empty
  meant any Host. A web page whose hostname re-resolves to `127.0.0.1` then
  becomes a same-origin client of an API that asks for no credential, and the
  MCP Origin check does not stop it because it accepts the origin the Host
  header claims. Reproduced on a live server: a request with Host and Origin set
  to `attacker.example` completed an MCP `initialize` and listed
  `/auth-profiles`. With `CONTROLLER_ALLOWED_HOSTS` unset and no credential
  configured, only loopback Host names are now answered (`/healthz` exempt);
  deployments with a token are unaffected.

  `CONTROLLER_ALLOWED_HOSTS` matching also moved off Starlette's
  `TrustedHostMiddleware`, which took everything before the first colon as the
  host — `[::1]:8000` became `[` — so the `::1` entry shipped in compose and
  `.env.example` never matched and IPv6 loopback clients were refused.

- **Browser downloads could run script as the controller.** `/artifacts`
  serves files the browser produced, including downloads whose name and bytes a
  visited site chooses, from the controller's own origin. A downloaded `.html`
  or `.svg` therefore ran as the controller; on a tokenless loopback
  deployment, opening one let it call every route, auth-profile export
  included. Reproduced in Chromium: the downloaded page's
  `fetch('/auth-profiles')` succeeded. `/artifacts` responses now carry a
  `sandbox` Content-Security-Policy (opaque origin, no script; images still
  render), and every response gets `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`.

- **Record ids from MCP arguments could name files outside their store.** The
  session, agent-job and approval file stores and the witness recorder built
  paths as `root / f"{id}.json"`. REST path parameters cannot carry `/`, but MCP
  tool arguments can, so ids like `../planted` read a file next to the store
  root and returned it as the store's own. Ids must now be one plain name; any
  other id is simply not found.

- **A mesh grant's `url_allowlist` was bypassable two ways.** Only top-level
  `url`/`start_url` arguments were checked, so `browser.execute_action` — whose
  target sits at `action.url` — was never checked; and patterns were matched
  with `fnmatch` over the whole URL, where `*` also matches `/`, `?`, `#` and
  `@`, so `https://*.example.com/*` admitted `https://evil.com/.example.com/`.
  URL arguments are now collected at any depth, and patterns match
  component-wise. **Behaviour change:** a pattern without a scheme (such as
  `*.example.com`) now matches the host only, and a URL with userinfo never
  matches. Mesh is opt-in (`MESH_ENABLED`).

- **Chromium and the display stack no longer run as root.** Chromium runs
  without its own sandbox in the container (Docker's default seccomp profile
  leaves it no user namespace to build one in), so root inside the container
  was the whole of what a renderer exploit started with. Chromium, Xvfb,
  x11vnc and noVNC now run as the unprivileged `browser` user (uid 10001); the
  entrypoint uses root only to take ownership of the mounted data directories.
  **Upgrading:** `./data/browser-profile` and `./data/downloads` become owned
  by uid 10001 on the host.

- **Optional VNC authentication.** noVNC (6080) and raw VNC (5900) had no
  authentication at all, which is safe only while both stay on loopback.
  `VNC_PASSWORD` now enables VNC authentication on the shared browser-node and
  on isolated session containers, and noVNC prompts for it. It is a second
  layer, not a gateway: the VNC protocol uses only the first 8 characters.

- **Observations named form fields by what was typed into them.** The
  interactables, active-element and form-outline scripts named a field by
  `aria-label`, placeholder, text and then its **value**. A login form's
  fields read `you@example.com`, `pw` and `on` instead of Email, Password and
  Remember me. A password typed into a field without a placeholder became that
  field's name in every later observation, reaching model prompts, MCP clients
  and logs, even though the type action had redacted it. Fields are now named
  the way a person reads them: `aria-labelledby`, `aria-label`, their
  `<label>`, button captions and image `alt`. A field's value is never its
  name. The same holds for rich-text editors (`contenteditable`) and
  textbox-like ARIA widgets, whose text content is what was typed into them.
  Interactables also report ARIA roles (textbox, checkbox, combobox, …) and
  checked state. The three scripts share one naming helper, checked under Node
  and in a real Chromium.

- **Approving a governed tool call approved any arguments.** The stand-in
  decision an approval is matched against carried only the tool name, and
  matching ignores the reason. An operator approving `browser.eval_js`
  approved code they never saw, and the approval covered whatever expression
  came next. The canonical arguments are now hashed into the decision, so an
  approval covers exactly one call. The operator reads a preview of the
  arguments, with cookie and storage values and other credential fields
  redacted.

### Fixed

- **An omitted `session_id` could target a closed session.** Resolution
  counted persisted records of closed and interrupted sessions, so after one
  close it picked the dead session or reported the call "ambiguous". With
  `MAX_SESSIONS=1`, every create → close → create cycle broke the
  convenience. Only live sessions are candidates now.

- **An unusable session id was its own error message.** A lookup raised
  `KeyError(session_id)`, which MCP surfaced as `{"error": "ec7fba8336ac"}`.
  The error now says why: the session is closed, interrupted (its browser is
  gone), recorded as active but not running on this controller (with a shared
  session store, another instance may own it), or unknown. It carries a code
  (`session_closed`, `session_interrupted`, `session_not_live`,
  `unknown_session`) over MCP and REST. It is still a `KeyError`, so existing
  404 handling is unchanged.

- **`browser.eval_js` could only fail.** It required
  `workflow_profile=governed`, a parameter its schema never advertised. Every
  call now goes through approval: the first returns `approval_required` with an
  `approval_id`, and the retry carries it.

- **The accessibility outline has been empty since Playwright removed
  `page.accessibility`.** Every observation reported `available: false`. The
  documented accessibility tree was missing from every preset, the OCR skip for
  pages with extracted text never applied, and the `accessibility_focus_changed`
  verification signal could not fire. The unit tests faked the removed API, so
  the bump passed.
  The outline is rebuilt from `page.aria_snapshot()`. That snapshot includes
  what each field holds (a filled password field reads
  `textbox "Secret": hunter2`), and the old outline copied a `value` for every
  node, so the new one keeps roles, accessible names and states only. A large
  page's snapshot (3 MB for 20,000 links) is parsed off the event loop. A test
  now asserts that the pinned Playwright has the API.

- **`scripts/fixture_live.py` has not worked since the DNS-rebinding guard.**
  Its TestClient sent `Host: testserver`, which a tokenless controller now
  refuses unless `CONTROLLER_ALLOWED_HOSTS` lists it. The script reported the
  resulting 400 as "SKIP: live browser stack unavailable". It now uses a
  loopback base URL, and a 4xx from the controller is a failure rather than a
  skip. All six fixtures pass live.

- **Policy refusals reached MCP agents as "Tool execution failed".** Some
  refusals raise `PermissionError`: a host outside `ALLOWED_HOSTS`, a retry
  with an approval that is not granted yet, a path outside its root. The
  gateway did not surface those, so a blocked navigation looked like a crash.
  They now return their reason with code `not_permitted`. Lookups that miss
  return their reason with code `not_found`: a mistyped `auth_profile` in
  `create_session` now says "No saved auth profile 'shop'", and an upload
  names the file missing from the upload directory. Before, both said "Tool
  execution failed". Those messages name what the caller asked for instead of
  the resolved path under a data root. An upload through `execute_action` now
  checks the file before asking for approval, so an operator is no longer asked
  to approve an upload that could only fail. OS-level permission and file errors,
  which can name server paths, stay opaque. The browser's own errors come back
  with their reason too, code `timeout` or `browser_error`: a
  `wait_for_selector` timeout, an invalid selector, or an exception thrown by
  an approved `eval_js` expression. Playwright's call log and wrapper stack
  frames are trimmed. The `approval_required`
  payload also gains a top-level `approval_id`, which the tool descriptions
  tell callers to send back.

- **Page text lost its structure.** The observation's `text_excerpt` squashed
  every run of whitespace, so a table's cells, a list's items and separate
  paragraphs ran together into one line. Line breaks and innerText's tab
  between table cells are kept now.

- **Share links did not work for the people they were handed to.** With a
  bearer token configured, `/share/{token}` answered `401` to anyone without
  the API token, and the screenshot the viewer renders came from `/artifacts`,
  which needs the token too. The signed share token is now the credential for
  the `/share/{token}/...` routes. Because those routes are unauthenticated,
  they return only what the viewer renders. **Behaviour change:**
  `/share/{token}/observe` returns the session id, URL, title and a screenshot
  URL instead of the full observation, and screenshots are served through
  `/share/{token}/screenshots/{name}` for that session only. A non-ASCII token
  also produced a `500`; it is a `403`.

- **Cron webhook triggers were unusable over the API.** `POST /crons`
  generated a `webhook_key` and then masked it in the response, so the key the
  trigger endpoint demands existed only in the store file. It is now returned
  once, on creation. The trigger route also answered a non-object JSON body with
  a `500`, let a body `job_id` override the job in the path, and turned a
  non-ASCII key into a `500` instead of a `403`.

- **A failed auth-profile import with `overwrite=true` destroyed the existing
  login.** The old profile was deleted before extraction began, so an archive
  that failed part-way left a partial profile in its place. Imports now extract
  into a staging directory and swap it in only once every member is written.

- **The Glama inspection image has not started since 1.7.0.** Its entrypoint
  binds uvicorn with `--host 127.0.0.1`, which the controller cannot see, so it
  assumed the fail-safe `exposed` scope and refused to start without a token.
  The entrypoint now declares `API_BIND_SCOPE=loopback`.

- CDP element intelligence embedded the caller's selector in JavaScript with
  Python's `repr()`, which mis-encodes some characters (no string break-out was
  found by fuzzing, but the in-page query could target a different selector).
  It now uses a real JSON/JS string literal.

- The Python client's `stream_events` disabled the connect timeout along with
  the read timeout, so an unreachable controller hung it forever.

### Added

- **Screenshots as MCP image content.** `browser.screenshot` and observe's
  `fast` preset return the screenshot as an image block after the JSON text.
  Before, they returned only a path on the controller's disk and a URL on the
  controller, and a model behind an MCP client can open neither. The file is
  read back only from inside `ARTIFACT_ROOT`, up to 4 MiB.

- **`browser.read_download`.** Reads a captured download as text, paged: the
  latest one, or one by `download_id`. Files are read only from the session's
  own downloads directory. Binaries (PDF, XLSX, images) are refused with their
  artifact URL. UTF-8 and BOM-marked UTF-16 decode exactly, and other text is
  flagged `lossy`. As with page text from observe and `get_html`, contents are
  not PII-scrubbed.

- **A pending-approvals queue on the dashboard.** It shows the action and
  reason, with Approve and Reject buttons. Before, approvals appeared only
  inside a finished job's replay. Reasons are rendered as text, a sensitive
  action's typed text is never shown, and a governed tool call reads as the
  tool rather than as its stand-in digest.

### Changed

- **MCP results are compact.** Every session reference in a tool result was
  the full session record, about 1.9k characters. An action result also
  carried the page before and after the action, each with its own copy of
  that record.
  - Nested session summaries are now references (id, name, status, live,
    current URL, title, takeover URL), and `browser.list_sessions` returns
    references too.
  - `execute_action` drops the pre-action snapshot, which its `verification`
    already summarises.
  - observe drops per-observation remote-access diagnostics.
  - Interactables drop keys that are null for that element.

  Measured on a login form at the end of this pass, counting both result
  copies. The "after" figures include the restored accessibility outline,
  about 600 characters of real content that the "before" figures lacked.
  - `execute_action`: 19.6k → 9.4k characters
  - observe: 12.9k → 8.6k
  - screenshot text: 4.8k → 1.3k
  - `list_sessions`: 13k for 6 sessions → 1.6k for 7

  A compact result is a subset of the full one. `detail="full"` on observe,
  `execute_action` and `execute_approval` returns the full payload.
  `browser.get_session`, `browser.create_session` and REST responses are
  unchanged. The built-in providers' prompts use the same session reference.

- **The curated MCP profile lists the 20 tools a browsing agent needs.** It
  listed 37 tools, about 41k characters of `tools/list` carried on every
  request. Harness inspection, Witness verification and export, the readiness
  check, memory profiles, traces, the network log, the console, page-error and
  request-failure tails (observe carries the last ten), auth-profile
  inspection, drag-and-drop and viewport sizing are now in the `full` profile.
  The curated list is 25.0k characters. Calling a full-profile tool on a
  curated controller names `MCP_TOOL_PROFILE=full` instead of answering
  "Unknown tool". `/mcp/tools/call` and mesh delegation serve tools through the
  same profile, so a peer delegating one of the moved tools needs the full
  profile too.

- **The MCP server's instructions describe how the tools fit together.**
  Clients put these in the model's context on connect, and they were one
  generic sentence. They now cover the observe → execute_action loop, reading
  with `get_html(text_only=true)`, `find_elements` and `read_download`, the
  `approval_required` → `approval_id` retry, and human takeover. A test keeps
  every tool they name in the curated profile.

- **`browser.get_html` is bounded and paged.** It returned the whole
  serialized DOM or page text in one result, routinely megabytes. It now
  returns up to `max_chars` (default 20,000, at most 1,000,000) from `offset`,
  with `total_chars`, `truncated` and `next_offset`. With `text_only=true` it
  uses the same line-preserving text as the observation. Its deprecated
  `full_page` argument, which never did anything, is still accepted but no
  longer advertised in the schema.

- **The controller image leaves out the provider CLIs by default.** Node.js
  and the codex, claude and gemini CLIs (about 750 MB) are needed only for
  `*_AUTH_MODE=cli`. Build with `INSTALL_AGENT_CLIS=true` (read from `.env` by
  compose) to include them. `docker-compose.host-subscriptions.yml` sets it,
  and `bootstrap_cli_auth.sh` stops with instructions when it is missing. A
  missing CLI binary now names the setting in the production startup error and
  in `GET /agent/providers`.

- Host-side developer scripts require Python 3.11, the floor every package
  declares. They accepted 3.10.

- The unserved legacy dashboard page, `controller/app/ui/index.html`, is
  removed. `/ui` still redirects to `/dashboard`.

- `make test` and `make test-local` run pytest, as CI does. They ran
  `unittest discover -s tests`, which imports test modules outside the `tests`
  package, so `tests/__init__.py` (the suite's environment defaults) never ran
  and about 145 HTTP tests failed. It also skipped every pytest-style test,
  roughly a third of the suite. `make test-local` now runs all 1,091 tests.

- The three PyPI packages declare their license as the SPDX expression `MIT`
  instead of the deprecated `license = { text = "MIT" }` table, which
  setuptools stops building after 2027-02-18. Building them from source needs
  setuptools 77 or later (`auto-browser-client`, `auto-browser-mcp`) or
  hatchling 1.27 or later (`auto-browser-langchain`). pip's isolated builds
  fetch these automatically.

- Controller dependencies: starlette 1.3.1 → 1.6.0, uvicorn 0.52.0 → 0.52.4,
  pydantic-settings 2.14.2 → 2.15.0, cryptography 50.0.0 → 50.0.1, and ruff
  0.16.1 → 0.16.5 for development. Starlette 1.5.1 limits a `FileResponse` to
  100 byte ranges and rejects inverted ranges, and `/artifacts` is served
  through it.

### Documentation

- `docs/llm-adapters.md` now covers the OpenAI-compatible provider family,
  shows how to point `openai_compatible` at any gateway or self-hosted server,
  and states when a provider gets a named profile.

- `docs/mcp-clients.md` shows direct HTTP setup for Claude Code, Cursor and
  VS Code, with bearer and operator headers. It also covers pairing Auto
  Browser with a web-search MCP server, including the `ALLOWED_HOSTS` change
  that search results need.

- The README's release highlights were for v1.5.0 and sat above the
  quickstart. A short "Recent Changes" section now follows the first demo.
  ROADMAP is current again.

- `AGENTS.md` gives coding agents the repo's commands and the invariants CI
  enforces.

## [1.7.0] — 2026-08-08

Closes the design issues raised in
[GHSA-xmh3-cw7j-9gp5](https://github.com/LvcidPsyche/auto-browser/security/advisories/GHSA-xmh3-cw7j-9gp5)
that 1.6.1 fixed the patchable half of and listed the rest of as *Known and not
yet addressed*, plus one item from the same report that list omitted. Two of the
three witness limits documented in 1.6.0 are fixed as well.

The thread running through all of it: a control that fails open is not a control.
Authentication that switched itself off when no token was set, an operator
identity anybody could assert, profiles with no owner, a signature nothing
checked, and a sandbox flag that disabled the sandbox.

**If you publish the API anywhere other than loopback, read the first entry
before upgrading** — it is the one change that can stop an existing deployment
from starting, deliberately.

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

- **Two of the witness limits documented in 1.6.0 are now fixed.**

  *Chain forking* was a correctness bug rather than a limit. Appends were
  serialised with an `asyncio.Lock`, which orders tasks on one event loop —
  not threads in this process, and not other processes. Two workers
  (`AGENT_JOB_WORKER_COUNT > 1`, several uvicorn workers, or two replicas on a
  shared volume) could read the same head and write two receipts claiming the
  same predecessor. Reading the head, signing, and appending are now one
  critical section under an OS-level lock on the chain file, with an in-process
  thread lock inside it.

  *Tail truncation* was undetectable: drop the last k receipts and what remains
  is a shorter chain whose every signature still verifies. Each append now also
  updates an anchor file beside the chain holding the head hash and receipt
  count, and `verify()` compares them — the result gains an `anchor` block, and
  a truncated or rewritten chain reports `valid: false` with the reason.
  Chains written before this release report `anchor: {"status": "unanchored"}`
  and keep verifying, because a missing anchor is not evidence of tampering.

  This needs no third party. An attacker who rewrites the anchor as well still
  defeats it, which is what a genuinely external anchor is for; the anchor is a
  separate artifact so one can be added later without changing this shape.
  Key compromise remains a stated limit.

  Appends also stopped re-reading the whole chain to find its head.

- **Staged skill candidates are verified on read, not just signed on write.**
  Induction could sign a candidate envelope, but nothing on the read side ever
  verified one — so the signature was an assertion the artifact made about
  itself, the same shape as the pre-1.6.0 witness chain. Anyone able to write to
  the staging root could edit a staged skill, or drop a new one in, and it was
  served as a governed candidate carrying provenance.

  The registry now checks the signature before returning a candidate, and checks
  that the envelope matches the artifact it sits in — a genuine signature lifted
  from a different candidate is still a forgery. A candidate that fails is
  refused; `list` omits what it cannot verify. Deployments with no signer
  configured are unaffected: there is nothing to check, and `signed` on the
  candidate now records which case it was, so an unsigned envelope is no longer
  a dict shaped exactly like a signed one.

  Candidates induced from a mock run are marked `simulated`, inside the
  provenance hash and the envelope, so a skill that no browser ever executed
  cannot present itself as converged. The README's "signed provenance" claim is
  reworded to say what actually holds.

- **Codex no longer runs on the host with approvals and sandboxing disabled.**
  Both Codex paths — the `cli` provider adapter and the host bridge — passed
  `--dangerously-bypass-approvals-and-sandbox`, a flag whose own help text reads
  "EXTREMELY DANGEROUS. Intended solely for running in environments that are
  externally sandboxed". A model decision could therefore run any command on the
  host.

  Nothing about the work needed it: both paths hand Codex a screenshot and a
  prompt in an ephemeral temp directory and read back a JSON decision. Codex now
  runs with `-s read-only` by default. **`CODEX_SANDBOX_MODE`** widens that to
  `workspace-write` or `danger-full-access` if you need it, and
  **`CODEX_BRIDGE_ALLOW_HOST_EXEC=true`** restores the bypass for the case its
  help text actually describes — an environment that is itself sandboxed. That
  opt-in logs a warning at startup and on the bridge's stderr.

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
