# LLM Adapter Pattern

The browser harness is now **model-agnostic** and has a real orchestrator.

## Internal contract

Every provider adapter returns the same internal decision schema:
- `navigate`
- `click`
- `type`
- `press`
- `scroll`
- `upload`
- `request_human_takeover`
- `done`

The LLM does **not** talk to Playwright directly.

## Current implementation

The controller now includes:
- `ProviderRegistry` for `openai`, `claude`, and `gemini`
- provider adapters under `controller/app/providers/`
- `BrowserOrchestrator` for one-step or multi-step loops
- provider discovery endpoint: `GET /agent/providers`
- step endpoint: `POST /sessions/{session_id}/agent/step`
- run endpoint: `POST /sessions/{session_id}/agent/run`

## How a step works

1. capture a fresh observation
2. send screenshot + structured page state to the chosen model
3. parse a strict structured action
4. execute that action through the controller
5. store artifacts and logs

## Provider strategy

### OpenAI
Uses the Chat Completions API with:
- image input
- strict function calling
- one required tool: `browser_action`

### Claude
Uses the Anthropic Messages API with:
- image input
- one forced tool: `browser_action`

### Gemini
Uses the Gemini `generateContent` API with:
- image input
- `responseMimeType: application/json`
- `responseJsonSchema`

## Example step request

```json
{
  "provider": "openai",
  "goal": "Open the main link on the page and stop.",
  "observation_limit": 25,
  "context_hints": "Prefer element_id over selector."
}
```

## Example run request

```json
{
  "provider": "claude",
  "goal": "Fill the search field with `playwright mcp` and stop before submitting.",
  "max_steps": 4,
  "observation_limit": 25
}
```

## Safety behavior

The prompt tells all providers to choose `request_human_takeover` for:
- login
- MFA / 2FA
- CAPTCHA
- payments
- posting / sending
- uncertainty

Upload approval still stays in the controller, not the model.

## Important limits

- This POC still uses **one visible desktop**, so only one active session is safe.
- `element_id` values are **observation-scoped**.
- Provider calls are synchronous HTTP requests in the API process.
- Live provider execution depends on `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `GEMINI_API_KEY` being present in the controller container.

## Next production upgrades

- switch OpenAI from Chat Completions to Responses API if you want one modern multimodal path everywhere
- move provider calls into a queue/worker tier
- add retry / loop detection policies
- add MCP tool wrapping so external agents can call this controller as a shared browser tool
