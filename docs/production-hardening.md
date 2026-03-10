# Auto Browser Production Hardening Spec

## Goal

Ship Auto Browser as a safe **single-tenant private beta** first, then harden toward a broader internal production tool.

## Hard requirements for the private beta target

- Production startup refuses to boot without:
  - `API_BEARER_TOKEN`
  - `REQUIRE_OPERATOR_ID=true`
  - `AUTH_STATE_ENCRYPTION_KEY`
  - `REQUIRE_AUTH_STATE_ENCRYPTION=true`
  - request rate limiting enabled
- Request-rate limiting with 429 responses and reset headers
- Metrics endpoint for scraping and alert wiring
- Automated retention cleanup for:
  - artifacts
  - uploads
  - saved auth-state files
- Containerized CI for controller tests + compose validation
- A deployment/runbook document with exact credential handoff steps

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

## Acceptance criteria

- Local containerized test suite passes
- Compose configs render cleanly
- Startup policy validation fails closed in production mode
- Metrics and cleanup endpoints function with auth enabled
- Deployment doc is sufficient for credential handoff + live debugging
