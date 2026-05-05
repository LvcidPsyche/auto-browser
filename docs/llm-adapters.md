# LLM Adapter Pattern

The browser harness is now **model-agnostic** and has a real orchestrator.

## Internal contract

Every provider adapter returns the same internal decision schema:
- `navigate`
- `click`
- `hover`
- `select_option`
- `type`
- `press`
- `scroll`
- `wait`
- `reload`
- `go_back`
- `go_forward`
- `upload`
- `request_human_takeover`
- `done`

Each decision also carries a `risk_category`:
- `read`
- `write`
- `upload`
- `post`
- `payment`
- `account_change`
- `destructive`

The LLM does **not** talk to Playwright directly.

## Current implementation

The controller now includes:
- `ProviderRegistry` for `openai`, `claude`, and `gemini`
- provider adapters under `controller/app/providers/`
- `BrowserOrchestrator` for one-step or multi-step loops
- provider discovery endpoint: `GET /agent/providers`
- step endpoint: `POST /sessions/{session_id}/agent/step`
- run endpoint: `POST /sessions/{session_id}/agent/run`
- background job resume endpoint: `POST /agent/jobs/{job_id}/resume`

## How a step works

1. capture a fresh observation
2. send screenshot + structured page state to the chosen model
3. parse a strict structured action
4. execute that action through the controller, or queue approval if it is sensitive
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
  "workflow_profile": "governed",
  "max_steps": 4,
  "observation_limit": 25
}
```

`workflow_profile` defaults to `fast`. Use `governed` when an operator wants the
agent to inspect first, avoid ambiguous writes, and request human takeover around
sensitive account/payment/posting/destructive work.

## Background jobs and resume

Queued agent runs persist a compact checkpoint after every completed step. A
checkpoint stores status, action, reason, URL/title, and error summary without
raw provider text. If the controller restarts while a job is running, startup
marks that job `interrupted` and leaves its checkpoints in the job record.

Resume through REST:

```bash
curl -X POST http://localhost:8000/agent/jobs/<job_id>/resume \
  -H "Content-Type: application/json" \
  -d '{"max_steps": 4}'
```

The MCP full profile exposes the same behavior as `browser.resume_agent_job`.
Resume creates a new child job with `parent_job_id` set to the original job and
injects the checkpoint summary into `context_hints`.

## Safety behavior

The prompt tells all providers to choose `request_human_takeover` for:
- login
- MFA / 2FA
- CAPTCHA
- payments
- posting / sending
- uncertainty

Approval stays in the controller, not the model.

If a provider still proposes a sensitive side effect, the controller does not trust it blindly:
- uploads become approval queue items
- `post`, `payment`, `account_change`, and `destructive` decisions become approval queue items
- the step/run returns `approval_required`

## Important limits

- This POC still uses **one visible desktop**, so only one active session is safe.
- `element_id` values are **observation-scoped**.
- Provider calls are synchronous HTTP requests in the API process.
- Provider HTTP calls now retry `429` and `5xx` responses with exponential backoff using `MODEL_MAX_RETRIES` and `MODEL_RETRY_BACKOFF_SECONDS`.
- Live provider execution depends on either provider API keys or mounted CLI auth state under `CLI_HOME`.
- Session observations now also include `tabs` and `recent_downloads`, so providers can follow popup flows and stop after a file lands or an export completes.

## Next production upgrades

- switch OpenAI from Chat Completions to Responses API if you want one modern multimodal path everywhere
- add streaming/SSE on top of the current REST + MCP surfaces when clients need server-pushed events
