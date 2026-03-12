# Launch Notes

Use this when you are ready to make the repo public.

## Suggested one-line description

Open-source MCP-native browser agent with human takeover, approvals, and reusable auth profiles.

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

## Suggested launch bullets

- local-first
- MCP-native
- visual browser control
- bundled stdio bridge for Claude Desktop and other stdio-first MCP clients
- human takeover when pages get brittle
- reusable auth profiles
- approval and audit rails
- MCP + REST API

## Public launch checklist

- [ ] review README top section
- [ ] verify license and contribution docs exist
- [ ] confirm `.env` and data dirs are ignored
- [ ] run `make release-audit`
- [ ] confirm screenshots/GIF you want in README
- [ ] verify no secrets in git history for this branch
- [ ] prepare first 3 “good first issue” tickets
- [ ] choose whether to publish as release `v0.1.0`
