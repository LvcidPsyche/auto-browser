# Roadmap

This is the near-term direction for Auto Browser.

## Now (current in v1.0.5)

- stable local-first browser control
- reusable auth profiles + import/export
- human takeover via noVNC
- approvals and audit trails
- MCP transport + REST API with 30+ tools
- Docker-based isolated session mode
- CDP connect mode — attach to an existing Chrome
- Network inspector — request/response capture with PII scrubbing
- PII scrubbing layer — pixel redaction, console, network (16 pattern classes)
- Proxy partitioning — named proxy personas for per-agent IPs
- Shadow browsing — flip headless → headed for live debugging
- Session forking — clone auth state into a new branch session
- Playwright script export — session replay as runnable .py
- Shared session links — HMAC-signed TTL observer tokens
- Vision-grounded targeting — Claude Vision element identification
- Cron + webhook triggers — autonomous scheduled jobs
- MCP Resources Protocol — live browser state as subscribable resources
- Operator dashboard at `/dashboard` with SSE event stream
- Durable background-agent checkpoints with dashboard resume/discard/cancel controls
- Repeatable agent eval harness for provider and workflow-profile comparisons
- Local HTML fixture evals for release-critical browser workflows

## Next

- cleaner multi-tab / popup management
- MCP `resources/subscribe` push notifications (live browser state streaming)
- stronger trace viewer integration in operator dashboard
- auth profile setup wizard

## Recently Shipped

- v1.0.1 security hardening, auth-profile archive fixes, client SDK repair, and dashboard XSS cleanup
- v1.0.4 governed approval enforcement, default-off social/Veo3 surfaces, default-off stealth, broader eval coverage, and release audit hardening
- v1.0.5 extracted social/Veo3 from the shipped controller wheel, added fixture evals, and raised controller coverage to 80%
- Durable background-agent checkpoints with REST/MCP resume support
- Request-level `fast` and `governed` workflow profiles for agent runs
- Agent memory profiles for cross-session context persistence
- Deployment readiness advisor with compliance mode checks
- Compliance templates (HIPAA, SOC2, GDPR, PCI-DSS) via a single env var
- GitHub Codespaces one-click demo environment
- LangChain / LangGraph / CrewAI integration package
- Timing-safe bearer token comparison
- Haiku as the default vision targeting model

## Later

- richer workflow recipes and app-specific helpers
- hosted control plane
- enterprise deployment support
- stronger remote access ergonomics
- session recording / replay with step-level time travel

## Explicit non-goals

Auto Browser is not being built as:
- a stealth browser
- an anti-bot bypass tool
- a CAPTCHA solver
- an unauthorized scraping framework

## Product direction

The open-source core should be excellent on its own.

If the project commercializes later, the likely path is:
- hosted runners
- managed auth/session storage
- team features
- enterprise deployment/support
