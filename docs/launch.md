# Launch Notes

Use this when you are ready to make the repo public.

## Suggested one-line description

Give your AI agent a real browser — with a human in the loop.

## Suggested longer description

Auto Browser is an open-source MCP-native control plane for authorized browser workflows. It gives LLMs, MCP clients, and operators a shared Playwright-powered browser with screenshots, interactable observations, approvals, audit trails, human takeover, and reusable auth profiles.

## Suggested GitHub topics

- browser-automation
- playwright
- fastapi
- mcp
- ai-agents
- browser-agent
- llm-tools
- operator-tools
- noVNC
- docker
- self-hosted
- claude

## Suggested launch bullets

- local-first
- MCP-native
- visual browser control
- bundled stdio bridge for Claude Desktop and other stdio-first MCP clients
- human takeover when pages get brittle
- reusable auth profiles
- approval and audit rails
- MCP + REST API

## Current release tag

Use `v1.0.5` for current public release references.

## Suggested launch timing

- Tuesday to Thursday
- 8am–10am US Eastern for Hacker News
- post to HN + Reddit + X/Discord the same day

## Suggested first public issues

See `docs/good-first-issues.md`.

## Public launch checklist

### Pre-launch (do before going public)

- [ ] review README top section
- [ ] verify license and contribution docs exist
- [ ] confirm `.env` and data dirs are ignored
- [ ] run `make release-audit`
- [ ] record demo GIF: login → human takeover → save auth profile → reopen session
- [ ] embed GIF in README (above the fold, before quickstart)
- [ ] verify no secrets in git history for this branch
- [ ] prepare first 3 “good first issue” tickets (see `docs/good-first-issues.md`)
- [ ] create GitHub release `v1.0.5` with changelog
- [ ] add GitHub topics: `mcp`, `browser-automation`, `playwright`, `llm`, `claude`, `ai-agent`, `self-hosted`, `local-first`, `fastapi`, `docker`

### Day 1 launch (same day, in order)

- [ ] post Show HN — title: `Show HN: Auto Browser – open-source MCP browser agent with human takeover`
- [ ] post to r/LocalLLaMA
- [ ] post to Anthropic Discord `#show-and-tell` channel
- [ ] post X/Twitter thread with demo GIF
- [ ] submit PR to `awesome-mcp-servers` (https://github.com/punkpeye/awesome-mcp-servers)
- [ ] submit PR to `awesome-playwright` (https://github.com/mxschmitt/awesome-playwright)

### Day 2–7

- [ ] respond to every issue and comment within 24 hours
- [ ] publish one technical post: “How I built persistent browser auth profiles for AI agents” or “Why browser agents need a human takeover button”
- [ ] post to r/selfhosted (angle: local-first, no cloud, your data stays local)

### Second wave (after initial traction)

- [ ] write comparison post: “Auto Browser vs Browserbase — when self-hosted beats managed”
- [ ] submit to more awesome lists: `awesome-llm-tools`, `awesome-ai-agents`
