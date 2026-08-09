# Auto Browser Production Hardening Spec

## Goal

Ship Auto Browser as a safe **single-tenant private beta** first, then harden toward a broader internal production tool.

## Hard requirements for the private beta target

- Startup refuses to boot without `API_BEARER_TOKEN` (at least 32 characters)
  whenever the API is reachable off-box — that is, `API_BIND_SCOPE=exposed`,
  which is the default for any deployment that does not declare otherwise. The
  shipped base compose declares `loopback` to match its `127.0.0.1` publish
  mapping, which is the only reason `docker compose up` runs without a token.
- Production startup additionally refuses to boot without:
  - `API_BEARER_TOKEN`
  - `REQUIRE_OPERATOR_ID=true`
  - `AUTH_STATE_ENCRYPTION_KEY`
  - `REQUIRE_AUTH_STATE_ENCRYPTION=true`
  - `CONTROLLER_ALLOWED_HOSTS` configured for the controller ingress hostnames
  - request rate limiting enabled
- Operator identity is only authentication when it comes from a named
  credential (`API_BEARER_TOKENS=alice:token-a,bob:token-b`), which records
  `source: "token"` on the audit event. With the shared `API_BEARER_TOKEN`,
  `X-Operator-Id` is attribution only and is recorded as `source: "header"`.
  When a header disagrees with the authenticated operator, the credential wins
  and the claim is kept in `asserted_id`.
- Request-rate limiting with 429 responses and reset headers
- Metrics endpoint for scraping and alert wiring
- Automated retention cleanup for:
  - artifacts
  - uploads
  - saved auth-state files
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
  enter the token at the prompt instead of bookmarking it, and rotate
  `API_BEARER_TOKEN` if such a bookmark may have leaked.

## Acceptance criteria

- Local containerized test suite passes
- Compose configs render cleanly
- Startup policy validation fails closed in production mode
- Metrics and cleanup endpoints function with auth enabled
- Deployment doc is sufficient for credential handoff + live debugging
