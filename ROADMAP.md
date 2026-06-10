# Roadmap

This is the near-term direction for Auto Browser.

## Now (current in v1.2.1)

- PyPI packages: `auto-browser-client` SDK, `auto-browser-langchain` adapters, `uvx auto-browser-mcp` stdio bridge
- stable local-first browser control
- reusable auth profiles + import/export
- human takeover via noVNC
- approvals and audit trails
- Witness receipts with on-demand hash-chain verification (REST + MCP)
- MCP transport + REST API with 70+ tools (curated and full profiles)
- Docker-based isolated session mode with orphan reaping and resource limits
- Stage 0 convergence harness for governed skill induction
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

- v1.2.1 PyPI publishing: client SDK, LangChain adapters, and the `auto-browser-mcp` bridge via tag-triggered trusted publishing
- v1.2.0 witness chain verification, isolated-container resource limits + orphan reaping, dependency refresh, and unified UA pool
- v1.1.x policy presets (`strict`/`balanced`), per-session isolation audit + CI smoke, dependency-audit gates, and the 80% coverage gate
- v1.0.1 security hardening, auth-profile archive fixes, client SDK repair, and dashboard XSS cleanup
- v1.0.4 governed approval enforcement, default-off social/Veo3 surfaces, default-off stealth, broader eval coverage, and release audit hardening
- v1.0.5 extracted social/Veo3 from the shipped controller wheel, added fixture evals, and raised controller coverage to 80%
- Durable background-agent checkpoints with REST/MCP resume support
- Request-level `fast` and `governed` workflow profiles for agent runs
- Agent memory profiles for cross-session context persistence
- Deployment readiness advisor with compliance mode checks
- Policy presets (`strict`, `balanced`) via a single env var, with deprecated aliases for prior names
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
