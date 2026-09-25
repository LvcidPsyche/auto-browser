# Good first issues

Use these as public contributor tickets that match the current product surface.
(Earlier seeds from this file shipped: the auth profile setup wizard, the run replay
view, and live fixture execution all landed in v1.3.0; the MCP `resources/subscribe`
walkthrough is `examples/mcp-resources.md`.)

## 1. Improve multi-tab and popup recovery tests

- **Label:** `good first issue`
- **Scope:** add fixtures and regression coverage for popups, tab switching, closed tabs, and returning to the useful active tab
- **Why it matters:** real browser workflows often branch into new tabs before the agent can finish cleanly

## 2. Add example configs for more OpenAI-compatible endpoints

- **Label:** `good first issue`
- **Scope:** contribute tested `.env` examples and short docs for driving the browser through additional OpenAI-compatible endpoints (Together, Groq, Fireworks, LM Studio, …) via the generic provider added in v1.4.0
- **Why it matters:** the adapter is generic, but first-time operators still benefit from copy-paste configs proven against real endpoints

## 3. Raise controller coverage toward 85%

- **Label:** `good first issue`
- **Scope:** add focused tests for `BrowserManager`, startup extension wiring, network inspector paths, and route handlers without needing live browsers
- **Why it matters:** the 80% release gate held through the architecture split; the next useful ratchet is coverage on the lower-signal edge paths that remain
