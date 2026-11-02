#!/usr/bin/env python3
"""Live check for auto-browser PR #246 (issue #244): which models refuse a named tool_choice,
and would the PR's fallback catch their real 400.

Standard library only. Run:  python probe_tool_choice.py
It reads OPENROUTER_API_KEY from the environment, or asks for it without echoing it.
Writes probe_results.json (no key in it) to the current folder.
"""
import getpass, json, os, re, sys, time, urllib.error, urllib.request

BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
MODELS = sys.argv[1:] or [
    "anthropic/claude-opus-5.5",
    "anthropic/claude-sonnet-5.5",
    "anthropic/claude-fable-5.1",
    "anthropic/claude-haiku-5.5",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-chat",
]
MAX_TOKENS = 2048
NAMED = {"type": "function", "function": {"name": "browser_action"}}
# The request the controller's openai_compatible adapter sends (captured from PR #246 at 9171de3),
# without its image, model and tool_choice. Only max_tokens is added, to bound the cost.
PAYLOAD = json.loads('{"messages": [{"role": "system", "content": "You are the Auto Browser planner. Pick exactly one next action. Use the provided function tool for your answer, or if you cannot call tools, reply with only the matching JSON object."}, {"role": "user", "content": [{"type": "text", "text": "Choose exactly one next browser action.\\nRules:\\n- Use only the current observation. element_id values are observation-scoped.\\n- Prefer element_id over selector. Use coordinates only for click when no reliable locator exists.\\n- Never invent URLs, elements, or file paths.\\n- Always set risk_category. Use read for navigate/hover/scroll/wait/reload/go_back/go_forward/done, write for normal click/type/press/select_option, upload for file uploads.\\n- If an action would post/send/publish content, set risk_category=post.\\n- If an action would submit a payment/order, set risk_category=payment.\\n- If an action would change profile/settings/security/billing/account state, set risk_category=account_change.\\n- If an action would delete/remove/cancel/close something, set risk_category=destructive.\\n- If the goal is already complete, return action=done.\\n- If the next step involves login, MFA, CAPTCHA, payments, sending/posting, or you are uncertain, return action=request_human_takeover.\\n- For upload, use only an explicitly provided staged file_path.\\n- Use wait when the page is loading or a result is expected to appear shortly without interacting.\\n- Use reload, go_back, or go_forward only when that browser navigation is clearly the best next move.\\nGoal:\\nOpen the \'Learn more\' link\\n\\nContext hints:\\nNone\\n\\nPrevious steps (most recent last):\\n[]\\n\\nCurrent observation:\\n{\\"session\\": null, \\"url\\": \\"https://example.com/\\", \\"title\\": \\"Example Domain\\", \\"active_element\\": null, \\"text_excerpt\\": \\"Example Domain. This domain is for use in documentation examples without needing permission. Learn more\\", \\"dom_outline\\": null, \\"accessibility_outline\\": null, \\"ocr\\": null, \\"interactables\\": [{\\"element_id\\": \\"op-1\\", \\"label\\": null, \\"role\\": \\"link\\", \\"tag\\": null, \\"type\\": null, \\"disabled\\": null, \\"href\\": null, \\"bbox\\": null, \\"selector_hint\\": null}], \\"console_messages\\": [], \\"page_errors\\": [], \\"request_failures\\": [], \\"tabs\\": [], \\"recent_downloads\\": [], \\"takeover_url\\": null}"}]}], "tools": [{"type": "function", "function": {"name": "browser_action", "description": "Select the single best next browser action.", "parameters": {"additionalProperties": false, "properties": {"action": {"enum": ["navigate", "click", "hover", "select_option", "type", "press", "scroll", "wait", "reload", "go_back", "go_forward", "upload", "request_human_takeover", "done"], "title": "Action", "type": "string"}, "reason": {"maxLength": 1000, "minLength": 1, "title": "Reason", "type": "string"}, "confidence": {"anyOf": [{"maximum": 1, "minimum": 0, "type": "number"}, {"type": "null"}], "title": "Confidence"}, "risk_category": {"anyOf": [{"enum": ["read", "write", "upload", "post", "payment", "account_change", "destructive"], "type": "string"}, {"type": "null"}], "title": "Risk Category"}, "element_id": {"anyOf": [{"maxLength": 500, "minLength": 1, "type": "string"}, {"type": "null"}], "title": "Element Id"}, "selector": {"anyOf": [{"maxLength": 2000, "minLength": 1, "type": "string"}, {"type": "null"}], "title": "Selector"}, "x": {"anyOf": [{"type": "number"}, {"type": "null"}], "title": "X"}, "y": {"anyOf": [{"type": "number"}, {"type": "null"}], "title": "Y"}, "text": {"anyOf": [{"maxLength": 5000, "minLength": 1, "type": "string"}, {"type": "null"}], "title": "Text"}, "clear_first": {"title": "Clear First", "type": "boolean"}, "sensitive": {"title": "Sensitive", "type": "boolean"}, "key": {"anyOf": [{"maxLength": 120, "minLength": 1, "type": "string"}, {"type": "null"}], "title": "Key"}, "value": {"anyOf": [{"maxLength": 1000, "type": "string"}, {"type": "null"}], "title": "Value"}, "label": {"anyOf": [{"maxLength": 1000, "type": "string"}, {"type": "null"}], "title": "Label"}, "index": {"anyOf": [{"minimum": 0, "type": "integer"}, {"type": "null"}], "title": "Index"}, "delta_x": {"title": "Delta X", "type": "number"}, "delta_y": {"title": "Delta Y", "type": "number"}, "wait_ms": {"maximum": 30000, "minimum": 0, "title": "Wait Ms", "type": "integer"}, "url": {"anyOf": [{"maxLength": 2000, "minLength": 1, "type": "string"}, {"type": "null"}], "title": "Url"}, "file_path": {"anyOf": [{"maxLength": 500, "minLength": 1, "type": "string"}, {"type": "null"}], "title": "File Path"}, "recipient": {"anyOf": [{"maxLength": 200, "minLength": 1, "type": "string"}, {"type": "null"}], "title": "Recipient"}, "platform": {"anyOf": [{"maxLength": 120, "minLength": 1, "type": "string"}, {"type": "null"}], "title": "Platform"}, "username": {"anyOf": [{"maxLength": 500, "minLength": 1, "type": "string"}, {"type": "null"}], "title": "Username"}}, "required": ["action", "reason", "confidence", "risk_category", "element_id", "selector", "x", "y", "text", "clear_first", "sensitive", "key", "value", "label", "index", "delta_x", "delta_y", "wait_ms", "url", "file_path", "recipient", "platform", "username"], "title": "BrowserActionDecision", "type": "object"}, "strict": true}}]}')
# Same pattern and the same text as controller/app/providers/openai_compatible.py _rejects_tool_choice.
TOOL_CHOICE_MENTION = re.compile(r"tool[-_ ]?choice", re.IGNORECASE)


def extract_message(body):
    # Mirrors BaseProviderAdapter._extract_error_message.
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        text = " | ".join(str(p) for p in (error.get("message"), error.get("type"), error.get("status"), error.get("code")) if p)
        if text:
            return text
    message = body.get("message")
    return message if isinstance(message, str) and message.strip() else None


def adapter_would_retry(status, body, text):
    if status != 400:
        return False
    message = extract_message(body) or text[:300]
    haystack = message if not isinstance(body, dict) else f"{message} {json.dumps(body)}"
    return TOOL_CHOICE_MENTION.search(haystack) is not None


def post(key, payload):
    data = json.dumps(payload).encode()
    for attempt in (1, 2):
        req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=data, method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                text = r.read().decode("utf-8", "replace")
                status = r.status
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            status = e.code
        except urllib.error.URLError as e:
            return None, None, f"network error: {e.reason}"
        if status in (429, 500, 502, 503, 504) and attempt == 1:
            time.sleep(3)
            continue
        try:
            body = json.loads(text)
        except ValueError:
            body = None
        return status, body, text


def decision_of(body):
    try:
        msg = body["choices"][0]["message"]
        finish = body["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return "no choices", None
    calls = msg.get("tool_calls") or []
    if calls:
        try:
            return "tool call: " + json.loads(calls[0]["function"]["arguments"]).get("action", "?"), finish
        except (ValueError, KeyError, TypeError, AttributeError):
            return "tool call (unparseable arguments)", finish
    content = str(msg.get("content") or "").strip()
    if not content:
        return "empty reply", finish
    return ("content with JSON action" if re.search(r'"action"\s*:', content) else "content, no JSON action"), finish


def prices():
    try:
        with urllib.request.urlopen(f"{BASE_URL}/models", timeout=60) as r:
            return {m["id"]: m.get("pricing", {}) for m in json.load(r)["data"]}
    except Exception as exc:  # cost is informational only
        print(f"(could not read prices: {exc})")
        return {}


def main():
    key = os.environ.get("OPENROUTER_API_KEY") or getpass.getpass("OpenRouter API key (not echoed): ").strip()
    if not key:
        sys.exit("No key given.")
    price = prices()
    scrub = lambda s: s.replace(key, "[key]") if isinstance(s, str) else s
    results, total = [], 0.0
    print(f"{len(MODELS)} models; at most 2 billed calls each, output capped at {MAX_TOKENS} tokens.\n")
    for model in MODELS:
        row = {"model": model}
        status, body, text = post(key, {"model": model, **PAYLOAD, "tool_choice": NAMED, "max_tokens": MAX_TOKENS})
        row["named_status"] = status
        if status == 200:
            row["named_result"], row["named_finish"] = decision_of(body)
        else:
            row["named_error_body"] = scrub(text)[:2000]
            row["adapter_would_retry"] = adapter_would_retry(status, body, text or "")
        for b in (body,):
            u = (b or {}).get("usage") or {}
            p = price.get(model, {})
            total += u.get("prompt_tokens", 0) * float(p.get("prompt", 0)) + u.get("completion_tokens", 0) * float(p.get("completion", 0))
        status2, body2, text2 = post(key, {"model": model, **PAYLOAD, "max_tokens": MAX_TOKENS})
        row["unforced_status"] = status2
        if status2 == 200:
            row["unforced_result"], row["unforced_finish"] = decision_of(body2)
            u = body2.get("usage") or {}
            p = price.get(model, {})
            total += u.get("prompt_tokens", 0) * float(p.get("prompt", 0)) + u.get("completion_tokens", 0) * float(p.get("completion", 0))
        else:
            row["unforced_error_body"] = scrub(text2)[:2000]
        if status == 200:
            row["verdict"] = "named choice accepted: no fallback needed"
        elif status == 400 and row.get("adapter_would_retry") and status2 == 200:
            row["verdict"] = "FIXED by #246: named refused, matcher fires, unforced works"
        elif status == 400 and not row.get("adapter_would_retry"):
            row["verdict"] = "NOT FIXED: 400 does not mention tool_choice, matcher misses"
        else:
            row["verdict"] = "other: see statuses and bodies"
        results.append(row)
        named = f"{status}" + (f" {row.get('named_result')}" if status == 200 else f" retry={row.get('adapter_would_retry')}")
        unforced = f"{status2}" + (f" {row.get('unforced_result')} ({row.get('unforced_finish')})" if status2 == 200 else "")
        print(f"{model:32s} named: {named:28s} unforced: {unforced}")
        print(f"{'':32s} -> {row['verdict']}")
    with open("probe_results.json", "w", encoding="utf-8") as f:
        json.dump({"base_url": BASE_URL, "results": results, "approx_cost_usd": round(total, 4)}, f, indent=1)
    print(f"\nApprox. cost from reported usage: ${total:.4f}. Wrote probe_results.json (no key in it).")


if __name__ == "__main__":
    main()
