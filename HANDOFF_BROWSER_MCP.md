# Browser MCP handoff — 2026-09-21

## Owner outcome

Run a browser on the existing Hetzner server, initially one active human user.
The owner wants an invite-only service that can later host multiple fully
isolated users. Each user chooses and verifies their own email during invited
enrollment, logs into their own websites themselves, and completes sensitive
steps (email codes, authenticator codes, website 2FA) without disclosing codes
to an assistant. A fresh phone authenticator code is required **when opening a
new server browser session**, not per agent action or per task. Explicitly
closing the server session revokes agent access; closing a viewing tab must not
be silently treated as closing the server session. No arbitrary 15-minute task
limit. The assistant should provide the correct portal link and step-by-step
status, then detect completion and continue without asking the human to say
"done". Social Operator should expose this as an optional advanced capability,
not become the browser identity provider or store website secrets.
The owner additionally wants a simple reusable connector: give an assistant a
link, connect it as an individually authorized agent, approve access to the
currently human-opened browser session once, and let it navigate, inspect,
click, type ordinary non-secret input, and complete requested browser tasks
without another phone code for every action. Human-only steps are account
passwords, email/app one-time codes, and human-verification challenges. The
assistant should display the browser at those points, wait for the human to
complete them, observe the resulting page, and continue. It must not claim
to be human, solve CAPTCHAs, or click an attestation that it is a human.

## What is deployed and verified

- Existing Hetzner host: `204.168.150.160`; app root: `/opt/auto-browser`.
- Auto Browser has one browser node, controller, and approval broker. The
  broker exposes MCP at `http://approval-broker:18001/mcp` **only inside** a
  dedicated Docker bridge `hermes-browser-private`; its host port binds
  `127.0.0.1:18001` only.
- The running Docker container named `hermes` (image `hermes-stack-hermes`,
  Compose project `hermes-stack`) in `/opt/stone/hermes-stack` was
  attached to that bridge. A distinct named `hermes` bearer was generated in
  `/opt/auto-browser/.env`, and Hermes's persistent `/opt/data/config.yaml`
  contains the private MCP URL and bearer. Do not copy or print the bearer.
- The Hermes stack compose file now contains the durable bridge attachment;
  the prior file is backed up as
  `/opt/stone/hermes-stack/docker-compose.yml.bak-auto-browser-20260920`.
- `hermes mcp test auto_browser` connected and discovered eleven broker tools.
  `deploy/hermes-integration/verify-on-hetzner.py` verified token match,
  MCP initialize, tool discovery, and denial when no owner session exists.
  `deploy/hermes-integration/check-on-hetzner.sh` verified network membership,
  private port, and Hermes configuration. The unconfigured-TOTP smoke check
  confirmed agents cannot create sessions. No human has enrolled TOTP yet.
- Follow-up broker work was deployed and re-verified on 2026-09-21. It adds
  `browser.session_status`, session-generation-bound grant reuse, and
  agent-wide owner revocation for the current session. A process-local lock
  makes revocation wait for an in-flight action and blocks later actions. The
  portal URL is not configured because the public portal is not active yet.
- In the current single-owner pilot, possession of a named agent bearer is
  the agent authorization; `browser.request_access` is not an interactive
  owner-approval prompt. Never present it as one. The target multi-user public
  connector needs actual per-user OAuth consent and visible connection
  revocation. Opening a human session allows previously connected agents with
  valid credentials to act, unless their agent credential is revoked. The
  current owner request-revoke button blocks that agent only for the **current
  browser session**; permanent disconnect requires removing its named bearer
  from protected server configuration and recreating the broker. The eventual
  user-scoped OAuth connector needs a normal per-user disconnect control.
- This connects **that single server Hermes container**, not every Hermes,
  Codex, Claude, ChatGPT, or Social Operator instance.
  The Hermes dashboard is routed at `https://hermes.fareeqk.com`; no Telegram
  bot identity was verified during this inspection.
- Local tests: `python -m pytest approval_broker/test_app.py
  owner_gateway/test_app.py -q` passed (21 tests) after the grant and VNC
  session guard. The owner gateway remains staged, not publicly routed.

## Current security boundary and limitations

The broker has a single process-local `owner_session_id`, one TOTP seed, and
named agent bearer tokens. It is a safe single-owner pilot, **not** tenant
isolation. `SESSION_ISOLATION_MODE=shared_browser_node` and one browser node
cannot be advertised as isolated users. The staged `owner_gateway` checks a
single `OWNER_EMAIL`; do not deploy that gateway as the invite-only multi-user
product. The existing Cloudflare DNS token cannot administer Cloudflare Access
apps/policies (Access app listing returned 403). No public portal, public MCP
endpoint, OAuth issuer, or remote third-party connector is live.
Reserve distinct intended hostnames, without publishing them yet:
`https://secure-browser.fareeqk.com` for the human portal and
`https://mcp-browser.fareeqk.com/mcp` for user-authorized remote assistants.
These are design targets, **not working links**. The MCP hostname must expose
only the OAuth-protected MCP resource and its authorization metadata; the
human portal must not expose the raw broker.

Do not publish the broker, controller, CDP, noVNC, VNC, static bearer, or
staged gateway directly. Neither an agent's bearer nor the user's portal email
may grant access to a different user's browser. Treat all screenshots and page
content as user-private data; block cross-user listing, guessed IDs, and stale
grants. The assistant must never receive a TOTP seed/code or website password.

## Target architecture (next implementation)

1. **Invitation and identity.** Admin creates a single-use, expiring invitation
   for a user or organization. At the link, the user enters their own email,
   proves control via email code or magic link, and optionally enrolls a phone
   authenticator. Bind the accepted invitation to immutable user and tenant
   IDs. Rate-limit redemption, expire/revoke invitations, prevent reuse, and
   audit inviter, invitee, time, and result. No open self-registration.
2. **Per-user browser boundary.** Give each user a dedicated persistent
   browser profile and data directory, encryption/key scope, session state,
   grant set, and agent credentials. The current shared browser node and
   singleton broker must not serve a second user. The safest immediate
   implementation is one complete controller/browser/broker stack and private
   data volume per invited user; the upstream `docker_ephemeral` mode can add
   a separate browser container per session inside each stack. Merely creating
   separate Playwright contexts does **not** isolate the shared noVNC desktop,
   controller data roots, auth-profile directory, audit logs, jobs, or memory.
   Start capacity planning with one active user; do not promise five or twenty
   concurrent users until measured. Users may share the host egress IP
   initially, as the owner chose earlier.
3. **Session lifecycle.** The human authenticates to the portal and enters a
   fresh authenticator code to open a server browser session. A valid session
   stays usable for long tasks; no per-tool code. Human authorization of an
   agent should be reusable for that session, not require repeating approval
   for every task. Explicit close revokes all attached grants immediately.
   Reopening always requires a new code and a fresh agent grant.
4. **Agent connection.** Keep one logical MCP tool contract, but mint a
   distinct revocable agent connection per provider/installation and bind it
   to the verified user and tenant. Public web connectors need HTTPS plus
   proper per-user OAuth authorization; no shared static bearer in a public
   URL. A server-side agent on the private bridge can use a named bearer only
   for its explicitly bound user. Every request and grant validates both
   agent identity and user/tenant scope. "Any assistant" means any assistant
   with a compatible MCP client; a non-MCP product needs its own small adapter
   to the same scoped API. Do not call the current private bearer endpoint a
   universal link.
5. **Guided handoff.** Add safe MCP tools such as `browser.session_status`
   and `browser.request_access` that return a human-facing portal URL and a
   state (`invite_required`, `email_verification_pending`, `totp_enrollment`,
   `session_closed`, `approval_pending`, `ready`, `revoked`). The agent may
   present the URL and instructions; it cannot fill codes. Let it poll or
   subscribe to state changes, then continue once the user approves. Persist
   pending task state so a paused assistant can resume without asking the user
   to report completion. Never claim provider-side indefinite task execution
   unless that provider actually supports it.
6. **Sensitive actions.** Website logins and high-impact website actions
   remain human-reviewed where required. The browser's own site 2FA is
   separate from portal login and separate from session-opening TOTP. Preserve
   existing owner-only approval boundaries; do not let an assistant approve
   its own request.
7. **Login persistence.** The current pilot does **not** automatically save
   site cookies/storage when a browser session closes. Today an owner must
   explicitly save an auth profile and reopen from that profile. Implement a
   safe per-user autosave/reload path only after confirming encrypted profile
   storage and hard ownership. Never present an active-session cookie as a
   persisted login; preserve user-controlled logout/revocation.

## Provider-specific connection plan

- **Hermes on the same server:** connected now via the private MCP URL and a
  named bearer. Once a human session and access grant exist, it can use the
  broker tools. After multi-user work, migrate this bearer to a user-scoped
  connection; never let a global Hermes credential enumerate users.
- **Codex CLI/desktop/IDE:** supports Streamable HTTP MCP with OAuth or a
  bearer-token environment variable. Once the user-scoped public HTTPS/OAuth
  endpoint is ready, add it through the Codex MCP settings and authorize as
  that user. Codex MCP configuration can be global or project-scoped. Do not
  give Codex the current loopback/private URL or the Hermes token. For OpenAI
  private use, Secure MCP Tunnel is an alternative **after** a Platform tunnel
  and permissions are configured; it is not a universal public connector.
- **Claude web/Cowork/Desktop remote connector:** calls the MCP server from
  Anthropic's cloud, so it requires a reachable public HTTPS MCP endpoint and
  per-user authorization. The current private URL cannot be pasted into the
  connector. Add the deployed URL under Claude custom connectors and connect
  each invited user individually.
- **Social Operator:** expose an admin-controlled optional "advanced browser"
  capability. Its existing entrypoint is `app/operator/routes.py`'s
  `POST /api/operator/chat`, which checks workspace membership, rate limits,
  and billing before calling `service.chat` in `app/operator/service.py`.
  Authenticate there, derive `workspace_id` from the verified session, and
  add a bounded browser adapter inside the service/tool orchestration layer.
  Never take workspace/profile IDs from model output or the request body.
  `app/team/loader.py` is prompt assembly, not the connector. Backend feature
  flag and optional workspace setting default to off. Existing UI
  `app/web/templates/workspace.html` and `app/web/static/workspace.js` can
  show setup/approval state; never place a credential or MCP URL containing a
  token in browser JavaScript. Telegram also calls `service.chat`, so gate
  capabilities per surface and do not dump large browser observations into
  Telegram. Preserve Social Operator's existing action approval for publishing
  and other high-impact operations; the optional browser capability may
  perform permitted interactive tasks through the same user-scoped MCP grants,
  rather than being permanently read-only. Store a reference to the
  user-scoped browser connection, never website credentials or a global bearer.
  Use the same MCP contract, not a second browser automation implementation.

Official provider docs:

- https://learn.chatgpt.com/docs/extend/mcp?surface=cli
- https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp

## Acceptance for the next agent

- Prove one invited email can enroll and a non-invited email cannot; invitation
  cannot be reused or transferred after redemption.
- Prove two user identities cannot see each other's sessions, profile data,
  screenshots, requests, grants, or agent credentials even by guessed IDs.
- Prove an assistant cannot create a session, bypass phone verification, use a
  closed session, or approve its own request.
- Prove Hermes remains connected after container recreation and an agent
  cannot act until a human opens a fresh session and approves its request.
- Prove the agent can give the user a working portal link, observe a completed
  human step, and continue without a separate "I am done" message.
- Prove public Codex/Claude connector authentication per user before enabling
  either publicly; no bearer/secret leaks to browser UI, logs, git, or docs.
- Prove Social Operator's optional tool stays disabled by default and enforces
  its existing workspace/user authorization independently of the LLM.

## Operational caution

The current repo has untracked local deployment code and a public upstream;
do not push deployment secrets or custom code to that upstream blindly. The
server configuration has already been changed for the private Hermes pilot.
Read `DECISIONS.md`, `deploy/external-mcp.md`, the broker and owner gateway
tests, and the current live stack before continuing. Preserve all unrelated
server deployments and user data.
