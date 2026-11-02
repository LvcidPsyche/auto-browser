---
status: in-review
pr: 246
issue: 244
branch: claude/issue-244-tool-choice
---

# Forced tool_choice fallback in the OpenAI-compatible adapter (#244)

## Problem

`controller/app/providers/openai_compatible.py:205` always sends
`"tool_choice": {"type": "function", "function": {"name": "browser_action"}}`. Endpoints that refuse a
forced choice answer 400, the orchestrator records `error_code: 400`
(`controller/app/orchestrator.py:142`), and the agent never acts. 4xx is not retried
(`controller/app/providers/base.py:151`), and the content fallback only runs on a 200.

## Evidence (research workflow wf_7a3ddfad-97e, critic re-checked the load-bearing claims)

| Endpoint | named | auto / omitted | Source |
|---|---|---|---|
| DeepSeek `deepseek-flash`, `deepseek-v4-pro` (thinking on by default) | 400 | works | api-docs.deepseek.com/api/create-chat-completion: "`required` and named tool choices are not supported in thinking mode; the API returns a `400` error" |
| Claude Opus 5.5, Sonnet 5.5, Fable 5.1, Mythos 5.1 (Anthropic, OpenRouter) | 400 | works | platform.claude.com/docs/en/api/errors; OpenRouter claude-5-5 migration guide; OpenRouter endpoints metadata `function:false, auto:true` |
| vLLM 0.8.3 to 0.13.x without `--enable-auto-tool-choice` | works | 400 | vLLM v0.13.0 source: auto guard only; named path needs no parser |
| vLLM 0.14+ without both flags | 400 | 400 | vLLM v0.33.0 `renderers/online_renderer.py` |
| vLLM with flags, SGLang, TGI, xAI, Together, Fireworks, OpenAI models via OpenRouter | works (forced, schema-guaranteed on several) | works (unforced) | per-endpoint sources in the workflow journal |
| Omitted vs `"auto"` | | identical everywhere checked (vLLM's validator rewrites a missing choice to `"auto"`); one unconfirmed third-party report of MiniMax rejecting an explicit `"auto"` | vLLM v0.33.0 `protocol.py` `check_tool_usage` |

OpenRouter's error schema is `error: {code, message, metadata?}`, and upstream detail can land in
`error.metadata` rather than `error.message` (openrouter.ai/docs/api/reference/errors-and-debugging).
`_extract_error_message` reads only `error.message/type/status/code` (base.py:563-580), but
`ProviderAPIError.raw_error` keeps the whole body.

## Decision

Not the issue's one-line `"auto"`: it would turn vLLM 0.8.3-0.13 servers started without tool flags
from working into a 400 on every step, and drop forcing for every endpoint that honours it.

Instead:
1. Send the named choice first, as today.
2. If the endpoint answers 400 and the error (message or the whole `raw_error` body) mentions
   `tool_choice` / `tool choice`, retry that request once with `tool_choice` omitted.
3. If the retry succeeds, remember `(base_url, model)` on the adapter (adapters live for the process,
   `app_factory.py:70`) and send later requests for it without `tool_choice` from the start. If the
   retry fails, raise its error and remember nothing.
4. Omit rather than send `"auto"`: same behaviour everywhere checked, minus the MiniMax report.

A 400 is a rejected request (no generation, nothing billed), so the retry is safe.

## Out of scope (follow-ups for the summary)

- vLLM 0.14+ without flags rejects every tools-bearing request but `tool_choice: "none"`; a third rung
  that drops tools and relies on the content parser would make it work. Broken today, unchanged here.
- Re-asking once when an unforced reply is prose with no parseable JSON.
- A per-profile `*_TOOL_CHOICE` override.
- `deepseek-chat` (the default `DEEPSEEK_MODEL`) is absent from DeepSeek's current model list; whether
  it is still served needs a live (paid) call.

## Steps

- [x] Failing tests first (DeepSeek-shaped 400, OpenRouter-shaped 400 with the text only in metadata,
      memory across steps, unrelated 400 not retried, retry failure raises and remembers nothing,
      other models still forced, named choice still sent first).
- [x] Adapter change.
- [x] docs/llm-adapters.md, CHANGELOG `[Unreleased]`.
- [x] Mutation checks: 22 mutants in a scratch copy, all red. No fallback; message-only and
      raw_error-only matching; remember before the retry, never, or on a named success; always omit;
      "auto" instead of omit; retry any 400, any status, or 5xx; remembered pair still forced; a second
      retry; key by model only; the regex narrowed six ways; no log line; a log line with the base URL.
- [x] Production-shape check: a local HTTP server through `ProviderRegistry`, real httpx, three steps
      per scenario, 18-24.5 KB requests with a 1280x800 screenshot. main fails every step on DeepSeek
      thinking and on OpenRouter Opus 5.5 (whose message is only "Provider returned error"); the branch
      recovers on step 1 and sends one request per step after. vLLM 0.13 without flags and an endpoint
      honouring the named choice are unchanged; vLLM 0.14+ without flags fails on both, with two
      requests per step. The endpoints are fakes built from the documented errors, not live APIs.
- [x] Independent review: four lenses (correctness, endpoint regression, tests, docs); findings
      triaged into this branch or the follow-ups above.
- [x] PR #246. Next: the maintainer reviews and merges it; then move this plan to plans/done/.
