# Launch Post Drafts

## Hacker News

Title:

```text
Show HN: Auto Browser – an open-source MCP-native browser agent
```

Body:

```text
I open-sourced Auto Browser, a local-first browser agent that exposes a real MCP server on top of Playwright.

The goal is not “stealth automation.” It’s supervised browser workflows for sites and accounts you’re authorized to use.

What it does:
- MCP server + REST API
- bundled stdio bridge for Claude Desktop / stdio-first MCP clients
- screenshots + interactable observations
- human takeover via noVNC
- reusable auth profiles
- approvals and audit trails
- Docker-based isolated browser sessions

The demo flow that best explains it is:
1. log into Outlook once
2. save an auth profile
3. reopen a fresh browser session already logged in

I think the interesting part is the packaging:
browser agent as an MCP server, not just another automation script.

Repo:
https://github.com/LvcidPsyche/auto-browser
```

## Reddit / r/LocalLLaMA

Title:

```text
I open-sourced an MCP-native browser agent with auth profiles + human takeover
```

Body:

```text
Just shipped Auto Browser.

It’s a local-first browser control plane for authorized workflows:
- MCP server
- stdio bridge for Claude Desktop
- Playwright backend
- screenshots + page observations
- auth profiles you can save and reuse
- noVNC human takeover
- approval/audit rails for risky actions

The main use case is not scraping. It’s “log in once, save session, reuse it across agent workflows.”

Example:
- sign into Outlook manually once
- save `outlook-default`
- future sessions resume from that auth profile

Repo:
https://github.com/LvcidPsyche/auto-browser
```

## X / Twitter

```text
Open-sourced Auto Browser today.

It’s a local-first MCP-native browser agent for authorized workflows.

- Playwright + FastAPI
- real MCP server
- reusable auth profiles
- noVNC human takeover
- approvals + audit trails

Best demo: log into Outlook once, save the auth profile, reopen a fresh logged-in session later.

Repo: https://github.com/LvcidPsyche/auto-browser
```
