# Auto Browser Production Hardening Spec

## Goal

Ship Auto Browser as a safe **single-tenant private beta** first, then harden toward a broader internal production tool.

## Hard requirements for the private beta target

- Startup refuses to boot without `API_BEARER_TOKEN` (at least 32 characters)
  whenever the API is reachable off-box — that is, `API_BIND_SCOPE=exposed`,
  which is the default for any deployment that does not declare otherwise. The
  token may also be placed in `API_TOKEN_FILE`. The shipped base compose declares
  `loopback` to match its `127.0.0.1` publish mapping; with no `API_BEARER_TOKEN`
  the controller then generates a token in `API_TOKEN_FILE` (`./data/api-token`)
  and demands it, because every container on the compose network can reach port
  8000. `API_ALLOW_TOKENLESS_LOOPBACK=true` serves loopback without a token; it
  never applies to an exposed or production controller.
- Production startup additionally refuses to boot without:
  - `API_BEARER_TOKEN`
  - `REQUIRE_OPERATOR_ID=true`
  - `AUTH_STATE_ENCRYPTION_KEY`
  - `REQUIRE_AUTH_STATE_ENCRYPTION=true`
  - `CONTROLLER_ALLOWED_HOSTS` configured for the controller ingress hostnames
  - `ALLOWED_HOSTS` naming the sites the browser's top-level page may visit (not `*`); iframes and subresources from other hosts are not filtered
  - `SHARE_TOKEN_SECRET`, so share links survive a restart
  - request rate limiting enabled
- Keep `PRE_REQUEST_BLOCKING=true` (the default), so a navigation to a host off `ALLOWED_HOSTS` is stopped before
  it is sent rather than refused after it loads. With `CDP_CONNECT_URL` or `browser.cdp_attach`, start that Chrome
  with `--disable-features=BlockOriginHeaderModificationOnRedirect,AvoidCorsURLLoaderRestartOnRedirect` and set
  `CDP_BLOCK_ORIGIN_ON_REDIRECT_DISABLED=true`; without it, cross-origin 307/308 POST redirects can fail, and on
  Chrome 155 and later credentialed, preflighted cross-origin requests fail after a redirect, in the operator's
  own tabs too, while the gate is attached. What is stopped and what is not:
  [`docs/deployment.md`](./deployment.md#stopped-before-the-request-is-sent). On Docker Desktop, browser
  containers can reach that Chrome's remote-debugging port: keep it off a host whose browser containers load
  pages you don't trust ([`docs/deployment.md`](./deployment.md#per-session-networks-docker_ephemeral)).
- Operator identity is only authentication when it comes from a named
  credential (`API_BEARER_TOKENS=alice:token-a,bob:token-b`), which records
  `source: "token"` on the audit event. With the shared `API_BEARER_TOKEN`,
  `X-Operator-Id` is attribution only and is recorded as `source: "header"`.
  When a header disagrees with the authenticated operator, the credential wins
  and the claim is kept in `asserted_id`.
- Under named credentials, sessions and auth profiles belong to the operator
  who created them. Another operator's session is left out of listings and
  answers like a session that does not exist, together with its approvals,
  agent jobs, cron jobs and `/artifacts` files; nothing under `/artifacts` that
  is not inside a session directory with a stored record is served to anyone.
  Agent jobs run as the operator who queued them, and cron jobs fire as their creator, opening only the auth
  profiles their creator may open. Neither outlives that operator's named credential: a cron fire is refused and a
  queued job fails once `API_BEARER_TOKENS` no longer names them, as after a move to the shared token.
  Sessions created under the shared token are
  unowned and open to every operator. The audit log stays operator-wide (an
  approval's id, kind, origin and tool, and its decision's comment, included), and
  the takeover desktop is shared by every session on a browser node; use
  `docker_ephemeral` isolation to give each session its own.
- Each approval decision records `decided_by`, the operator a named credential proved (for an agent's decision, the
  operator it acts for), in the record, `GET /approvals`, the decision's audit event and its witness receipt's
  `metadata`; it is `null` without named credentials. `APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS=alice,ci-bot`
  refuses a listed operator an approve of an approval they asked for (`409`), the withdraw of a rejection waiting on
  it included; their rejections, and an agent's approval under `AUTONOMOUS_APPROVALS`, are not affected. Under named
  credentials only the operator who asked may use an approval in a session they own or in controller scope, so a
  listed operator's approvals there stay pending: list operators whose actions run in sessions created under the
  shared token, which every operator may use.
- `browser.cdp_attach` is refused under `BROWSER_SANDBOX=on`: the controller cannot verify the sandbox
  of a Chrome reached over CDP (over `POST /mcp/tools/call`, as every tool refusal, that is HTTP 200 with
  `isError`). `CDP_CONNECT_URL` is the operator's own way to use such a Chrome.
- `browser.cdp_attach` swaps the one browser every session runs on, so it is refused
  whenever more than one credential is configured (named credentials, or a named one
  next to the shared token): the attached browser would be shared by every operator.
  These two refusals (under `BROWSER_SANDBOX=on`, above, and with more than one
  credential) are the tool's guard. The attach URL is the caller's to name, as
  `CDP_CONNECT_URL` is the operator's, so it is not checked against `ALLOWED_HOSTS`.
  Run a separate controller per operator, or set `CDP_CONNECT_URL` at startup. With a
  single credential it is refused while live sessions still use the current browser,
  and the browser it replaces is disconnected.
- Governed approvals are the calling agent's choice unless the server makes them its own:
  `workflow_profile` is an argument the agent supplies and defaults to `fast`, so on a
  multi-operator deployment an agent that leaves it out can run ordinary writes without
  governed approval; the runtime approvals still apply (an upload's, with
  `REQUIRE_APPROVAL_FOR_UPLOADS` on by default, and those of `post`, `payment`,
  `account_change` and `destructive` decisions), and `browser.eval_js` always asks: an operator,
  unless `AUTONOMOUS_APPROVALS=true` lets the agent approve its own (below).
  `FORCE_GOVERNED_APPROVALS=true` takes the choice away. It is off by default because it
  stalls an existing agent loop on its first write until an operator approves. Readiness
  (`GET /readiness`, `browser.readiness_check`) warns `governed_approvals` when more than
  one operator credential is configured and it is off. What it covers and what it does not:
  - It covers every MCP tool call that has a governed kind (every cron tool but
    `browser.list_cron_jobs`,
    `browser.execute_action`, `browser.set_cookies` and `browser.delete_memory_profile` among
    them) and every step of the agent loop: MCP-queued steps and runs (`browser.queue_agent_step`,
    `browser.queue_agent_run`), REST `/agent/step` and `/agent/run`, and cron runs. Each runs as
    `governed` whatever `workflow_profile` the request carried, so an operator approves each write.
  - It does not cover the REST action routes (`/sessions/{id}/actions/*`). They take no
    `workflow_profile` and ask for approval only for uploads and for `post`, `payment`,
    `account_change` and `destructive` decisions, as before. An agent holding a bearer token can
    call them directly. The setting is a control on agents that act through MCP tools and the
    agent loop; it is not a sandbox for a token holder.
  - `AUTONOMOUS_APPROVALS=true` lets the agent approve its own pending actions
    (`browser.approve_approval`, and the agent loop itself), so with it on the forcing only adds
    a step the agent performs itself. Keep it off wherever `FORCE_GOVERNED_APPROVALS` is meant
    to be a control.
  - An approval of a tool that names no session (`browser.delete_memory_profile`,
    `browser.cdp_attach`) is held in a controller-wide scope and belongs to the operator who
    asked, as described in `docs/mcp-clients.md`.
- In the fast profile a click's `risk_category` is the model's word, and text on a page can steer the model, so a
  click labelled `read` or `write` (an unlabelled click is `write`) has its target read first, found as the click
  will find it. An `element_id` or a point is read in the controller's isolated world, a point through open shadow
  roots and into frames of the page's own origin (three deep). A selector is resolved by Playwright, as the click
  resolves it (CSS through open shadow roots, `text=`, `role=`, `xpath=`, `>>` chains), and its element is
  described where the page's script runs.
  - A button (`button`, an `input` of type `button`, `submit`, `image` or `reset`, or `role=button`; an element
    inside one counts as the button) asks for a kind's approval, as an honest label does, when its label (its
    `aria-label`, its visible text, or an input's value or alt text) starts, as whole words, with a phrase of that
    kind in `controller/app/browser/click_target.py`:
    - `payment`: `Pay`, `Buy`, `Purchase`, `Place order`, `Place your order`, `Order now`, `Confirm order`,
      `Confirm payment`, `Confirm purchase`, `Confirm and pay`, `Complete order`, `Complete purchase`,
      `Submit order`, `Submit payment`, `Donate`, `Transfer`
    - `destructive`: `Delete`, `Remove`, `Erase`, `Permanently delete`, `Yes, delete`, `Yes, remove`,
      `Confirm delete`, `Confirm deletion`, `Empty trash`, `Cancel subscription`, `Close account`, `Deactivate`
    - `account_change`: `Change password`, `Update password`, `Reset password`, `Change email`, `Update email`
    - `post`: `Publish`, `Post`, `Send`
    - Not asked about, though each starts with a phrase above: `Remove filter`, `Remove filters`,
      `Remove all filters`, `Remove from cart`, `Remove from basket`, `Remove from compare`,
      `Remove from wishlist`, `Remove from favorites`, `Remove from favourites`, `Purchase history`,
      `Purchase order`, `Purchase orders`, `Pay as you go`, `Post code`
    - Nor is `Send` asked about when its line goes on to one of: `code`, `link`, `otp`, `passcode`,
      `verification` ("Send code", "Send me a login link", "Send reset link")
  - A label is read as its whole text on one line, and then each of its first three lines by itself, so a name
    over a sentence ("Delete account" above "This permanently removes your profile") is read by its name. A line
    of over 40 characters is a sentence, not a name, and is not matched. Case and spacing do not count; zero-width
    characters and soft hyphens are removed; up to two leading tokens that are a number, a mark or an icon font's
    ligature name are skipped ("1 Delete", "credit_card Pay now"), and a label that is one ligature name is read
    with its underscores as spaces ("delete_forever"); a hyphen goes on with its word ("Pay-as-you-go" is not
    "Pay"); the comma of `Yes, delete` may be missing.
  - Left out on purpose: bare `Close`, `Cancel`, `Dismiss`, `OK`, `Continue`, `Next` and `Accept` (they are
    everywhere and mostly harmless), `Checkout` and `Proceed to checkout` (they open a page), `Subscribe`,
    `Unsubscribe`, `Submit`, `Confirm`, `Save changes` and `Clear`.
  - A form's submit button with no such label, labelled `read`, is recorded as `write`, which asks for nothing, so
    search boxes and login forms keep working unattended: `Search`, `Sign in`, `Continue` and `Send code` run. A
    form whose button says `Reset password` asks, as the table says. One labelled `write` is untouched.
  - An approval asked for this way says so: its reason opens with the controller's sentence, which names the kind
    the target reads as and the label the agent gave the click, and the agent's own reason follows, named as the
    agent's. Nothing read from the page is in it.
  - A click by coordinates is pressed on the element that was read: the press lands a little off its point, as a
    hand's does, only while the same element is under it, and on the point itself otherwise.
  - It does not catch: a label the table lacks, the phrases left out above among them; a frame of another origin
    (a point inside one is read as the frame), and a frame that CSS scales or rotates (read at a point that is
    off); a control pressed by a key (Enter or Space on a focused button, Enter in a form field) or by a drag; a
    clickable element that is not a button (a `div` with a handler and no role) and a link ("How to delete your
    account" is an article); a label given only by `aria-labelledby`, by an image, or by text that CSS or a canvas
    draws; look-alike characters (a Cyrillic "е" in "Delete"); and a page whose own script falsifies the read of a
    selector's element, or changes the target between the read and the press, or between the ask and the retry.
    Such a page can as well name its button "Continue": the read answers text that steers the model, not a site
    that is itself hostile.
  - A target that is not there (no element at the point, an `element_id` the page no longer has) is not
    classified, and neither is one that cannot be read: a page that does not answer within 2 seconds (for an
    `element_id` or a point, 2 to set the read up and 2 more to run it), or a selector that matches nothing in
    those 2 seconds. Either way the click runs as labelled; when the read failed, one info-level log line names
    the session and the kind of failure. Inside a governed run's approval, which an operator gave for that very
    click, it is not read again.
  - A navigation off `ALLOWED_HOSTS` needs no rule of its own: with `PRE_REQUEST_BLOCKING` on (the default) it is
    refused whatever its label.
- Request-rate limiting with 429 responses and reset headers
- Metrics endpoint for scraping and alert wiring
- Automated retention cleanup for:
  - artifacts
  - uploads
  - saved auth-state files
  - decided approvals, deleted `APPROVAL_RETENTION_DAYS` (30 by default; `0` keeps them) after their decision. Kept
    whatever its age: an approval still pending, held by an action, or approved and not expired, and an operator's
    rejection in controller scope or in a session still open, which goes on refusing an agent's approval of the
    same action there.
- Containerized CI for controller tests + compose validation
- A deployment/runbook document with exact credential handoff steps
- `STEALTH_ENABLED=false`

## Non-goals for this phase

- stealth or anti-bot evasion
- full multi-tenant SaaS isolation
- SSO / RBAC / enterprise IAM
- HA-grade data plane and database failover

## Current constraints

- Docker-based isolation is appropriate for trusted single-tenant use, not hostile multi-tenant SaaS
- CAPTCHAs, MFA, and brittle login flows still require human takeover
- noVNC must sit behind a real access layer before remote use
- File + SQLite durability is acceptable for beta, not enough for larger-scale production
- Social/Veo3 integrations are extracted from the shipped controller package and are not part of the production surface.
- The dashboard accepts `#token=...` in the URL hash for one-click bookmarkable access.
  The fragment never reaches server logs, but a bookmarked or history-persisted URL
  stores the bearer token in the browser profile. On shared or remote machines,
  enter the token at the prompt instead of bookmarking it, and rotate the token
  if such a bookmark may have leaked (`API_BEARER_TOKEN`, or delete
  `data/api-token` and restart the controller to generate a new one).

## Acceptance criteria

- Local containerized test suite passes
- Compose configs render cleanly
- Startup policy validation fails closed in production mode
- Metrics and cleanup endpoints function with auth enabled
- Deployment doc is sufficient for credential handoff + live debugging
