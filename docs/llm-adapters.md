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
- `ProviderRegistry` for `openai`, `claude`, `gemini`, and the OpenAI-compatible
  family (`openrouter`, `xai`, `deepseek`, `minimax`, `openai_compatible`)
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

Each of the three first-party providers has an `api` mode (a key in the environment)
and a `cli` mode (the vendor's CLI, signed in under `CLI_HOME`: `codex`, `claude`,
`gemini`). OpenAI also has a `host_bridge` mode that reaches a host-side Codex over a
Unix socket. `GET /agent/providers` reports the mode each provider is in.

### OpenAI
Uses the Responses API (`POST {OPENAI_BASE_URL}/responses`) with:
- image input (`input_image`)
- strict function calling
- one required tool: `browser_action` (`tool_choice` names it)

Not Chat Completions: current OpenAI models refuse function tools there while they reason.

### Claude
Uses the Anthropic Messages API with:
- image input
- one tool, `browser_action`, requested rather than forced: `tool_choice` is `auto`
  with `disable_parallel_tool_use`, because Opus 5.5, Sonnet 5.5 and Fable 5.1 reject a
  forced `tool`/`any` choice with a 400, and the system prompt asks for the call
- `max_tokens` of 16,000, since thinking counts against it

### Gemini
Uses the Gemini `generateContent` API with:
- image input
- `responseMimeType: application/json`
- `responseJsonSchema`

### OpenAI-compatible providers
One shared adapter (`controller/app/providers/openai_compatible.py`) drives every
provider that speaks the OpenAI Chat Completions dialect (`POST {base_url}/chat/completions`,
unlike the OpenAI provider above, which uses the Responses API): screenshot input
(`image_url`) and a required strict `browser_action` tool, no `temperature`, with a
fallback that parses the decision from message content for endpoints that ignore
`tool_choice`. The `deepseek` profile is text-only, so it gets no screenshot and
works from the page outline.

Named profiles exist for `openrouter`, `xai`, `deepseek` and `minimax`. Anything
else — a hosted gateway or router, a self-hosted Ollama / vLLM / LM Studio server,
Azure OpenAI, Together, Groq, Fireworks — uses the `openai_compatible` slot with
no code change:

```env
OPENAI_COMPATIBLE_BASE_URL=https://gateway.example.com/v1   # the URL that /chat/completions hangs off
OPENAI_COMPATIBLE_API_KEY=...                               # sent as Authorization: Bearer
OPENAI_COMPATIBLE_MODEL=provider/model-id
```

then pass `"provider": "openai_compatible"` in agent requests.
`GET /agent/providers` reports whether the slot is ready and what is missing.

**Adding a named profile.** The named list is kept short and vendor-neutral on
purpose: every entry adds three settings, three compose variables and a default
model that goes stale and has to be kept current (the compose-vs-config parity
test exists because it once did). A profile is worth that when the provider needs
something the generic slot cannot express — a text-only model family, as with
`deepseek` — or when a large share of users already run it. A gateway that works
through `openai_compatible` unchanged is documented rather than given its own
profile; that is a statement about maintenance cost, not about the gateway.

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
agent to inspect first and require approval before write-sensitive actions.

With `FORCE_GOVERNED_APPROVALS=true` the controller ignores this field and runs every step of
the loop as `governed`, whether the run came from REST `/agent/*`, a queued MCP job or cron; the
same setting forces `governed` on MCP tool calls. It does not cover the REST action routes, which
an agent holding a bearer token can still call directly (see `docs/mcp-clients.md`).

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

If a provider still proposes a side effect, the controller does not trust it blindly:
- in `fast`, uploads and high-risk `post`, `payment`, `account_change`, and `destructive` decisions use the approval queue
- in `governed`, every non-read decision requires approval, including ordinary click/type writes
- the step/run returns `approval_required` until an approval token is supplied
  (with `AUTONOMOUS_APPROVALS=true` the agent approves its own action instead; the
  approval is still recorded)

## Important limits

- By default (`SESSION_ISOLATION_MODE=shared_browser_node`) all sessions share one Chromium
  and one noVNC desktop: cookies and storage stay separate per session, but a human taking
  over one session sees the others' windows. `docker_ephemeral` gives each session its own
  browser container (see `docs/session-isolation-audit.md`).
- `element_id` values are **document-scoped**: each names an element of the document it was observed in. The
  controller keeps them in an isolated world page scripts cannot read or change, not in the page, and refuses an
  id from an earlier document or for an element the page removed or moved into another document, a frame's
  (`stale_element_id`), one the tab was never given (`unknown_element_id`; past the last 10,000 ids a tab was
  issued, stale and unknown are told apart best-effort) or one not in the form observe issues
  (`invalid_element_id`). Observe again and use an id from the new observation.
- What an `element_id` guarantees: the page cannot choose, forge or copy which element it names. Click, hover,
  type and select by `element_id` find and check that element in the isolated world and aim the mouse or keys at
  it: they wait until it is visible, stable (the same box in two consecutive animation frames; on a page that
  draws no frame at all for 1 s, such as a hidden tab, two checks 50 ms apart; a wait whose frames stop partway is
  refused as `element_not_stable` at its deadline), in view for click, hover and type, and enabled, by
  Playwright's rules, for click, type and select (type waits up to 1 s, then focuses the field directly); aim
  within 2.5 px, in each direction, of the centre of its first client rect inside the viewport (a hover at the
  centre itself); press, or hover, only where the element itself takes the pointer (a label for it, an embedded
  frame in the document or its open shadow roots, or anything over it is refused as `element_covered`, naming the
  cover); check that keyboard focus is in it (not in a frame inside it, open shadow roots included) before keys go
  out; and add no attribute or node to the page. A selection or an upload by the `element_id` of a label, or of an
  element in a label that is not a control itself (Playwright's 'follow-label' rule), goes to the `<select>` or
  file input the label is for when the action starts, as Playwright's does; upload marks that input for the one
  action. `browser.find_elements` and `browser.wait_for_selector` refuse a `selector_hint`; pass `element_id` to
  the action tools.
- What it does not guarantee is delivery against a page that runs script in the gaps between the checks and the
  input: the few milliseconds between the last hit test and the press, and the 20-80 ms between mouse down and up;
  focus is checked before the text, between select-all and delete and after the clear, not between keystrokes, and
  a keydown handler can move focus within the key it handles, the clear's Delete included: the check after the
  clear then reports `focus_lost` after the field it moved to was emptied; hover is not hit-tested once the
  pointer arrives; a drag's destination is not hit-tested (drag libraries put the dragged element under the
  pointer), and its check of a coordinate end compares scroll positions, so a page can still change what is under
  that point (by scrolling a box no element end is drawn inside while stopping its scroll events or inside a
  shadow root, by scrolling back to where it was, or by replacing content), and a scroller an element end is
  slotted into through a closed shadow root is not seen; the hit test and the focus check do not see into closed
  shadow roots, so an embedded frame behind one inside the element takes the click (as with Playwright or a person
  clicking), and focus in such a frame reads as focus on the element, so typed text goes to the frame; and between
  the check before Playwright resolves the marked file input and the check after it, a page script can move the
  marker to another file input of its own document, so that the upload goes there, and then put it back. An upload
  is never a retryable failure once the file has gone in: by `element_id` it records `target.delivery` as
  `confirmed` (the file input holds one file of that name and size) or `not_confirmed` with a reason, best-effort,
  since the page can give the input a file of the same name and size (by selector there is no `target.delivery`);
  a step that fails after the transfer with a browser error (the page closing while it settles) is reported in
  `target.after_change`, and the upload must not be repeated. An error that is not retryable (a policy block, a
  bot challenge) still surfaces after the transfer, and so does a cancelled request; the file went in all the
  same.
- Each message to the isolated world has a deadline, `ACTION_TIMEOUT_MS` plus its own wait for a script that waits
  on the element: a bound per message, not a deadline for the whole action. A page that does not answer in time
  fails with `page_not_responding` (not retryable; HTTP 504 over REST, an MCP tool error with that code). Other
  calls still wait on the page (its title, mouse and keyboard input, the checks after an action), so a page that
  hangs in its click handler holds that action until it answers. A change sent to the world (a focus, a selection,
  the upload's marker) whose answer is lost after it was sent fails as `change_unconfirmed` (not retryable):
  observe the page before acting again.
- Provider calls are awaited HTTP requests made from the API process; there is no separate model worker.
- Provider HTTP calls retry timeouts, network errors, `429` and `5xx` responses with exponential backoff (`MODEL_MAX_RETRIES`, `MODEL_RETRY_BACKOFF_SECONDS`); a `Retry-After` header on a `429`/`5xx` lengthens the wait (capped).
- Live provider execution depends on either provider API keys or mounted CLI auth state under `CLI_HOME` (OpenAI can also use `host_bridge`).
- Session observations now also include `tabs` and `recent_downloads`, so providers can follow popup flows and stop after a file lands or an export completes.

## Next production upgrades

- add streaming/SSE on top of the current REST + MCP surfaces when clients need server-pushed events
