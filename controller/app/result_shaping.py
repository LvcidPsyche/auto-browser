"""Compact projections of controller results for model consumers.

Controller results are built for operators and audit logs. Every session
reference carries the full session record — isolation roots, auth-state
metadata, witness delivery status, remote-access diagnostics, about 1.9k
characters — and an action result carries the page both before and after the
action, each with its own copy of that record. A model pays for every character
on every step, and none of that metadata helps it choose the next action: one
typed field cost about 10k characters of context.

MCP tool results and the built-in providers' prompts use the projections below.
REST responses, action logs and witness receipts keep the full payloads, and an
MCP caller that wants them passes ``detail="full"`` or calls
``browser.get_session``.

A compact result is always a subset of the full one: every key it keeps has the
same name and meaning, so a caller written against either shape reads the
other.
"""

from __future__ import annotations

from typing import Any, Literal

ResultDetail = Literal["compact", "full"]

# Enough to refer to a session and to judge whether it can be acted in.
SESSION_REFERENCE_KEYS = ("id", "name", "status", "live", "current_url", "title", "takeover_url")

# Keys every session summary carries; how one is recognised in a result.
_SESSION_SUMMARY_KEYS = frozenset({"id", "status", "live"})

# Per-observation diagnostics for the operator's remote takeover path. The
# takeover URL a model may hand to a human is kept at the top level.
_OBSERVATION_OPERATOR_KEYS = frozenset({"remote_access"})


def is_session_summary(value: Any) -> bool:
    return isinstance(value, dict) and _SESSION_SUMMARY_KEYS <= value.keys()


def compact_session(session: Any) -> Any:
    """Reduce a session summary to a reference. Anything else passes through."""
    if not is_session_summary(session):
        return session
    return {key: session[key] for key in SESSION_REFERENCE_KEYS if key in session}


def compact_interactable(item: Any) -> Any:
    """Drop the keys that do not apply to this element (``href`` on a button, ``checked`` on a link).

    Only ``None`` goes: ``checked: false`` and ``disabled: false`` are state.
    """
    if not isinstance(item, dict):
        return item
    return {key: value for key, value in item.items() if value is not None}


def compact_observation(observation: Any, *, keep_session: bool = True) -> Any:
    if not isinstance(observation, dict):
        return observation
    compact = {key: value for key, value in observation.items() if key not in _OBSERVATION_OPERATOR_KEYS}
    if "session" in compact:
        if keep_session:
            compact["session"] = compact_session(compact["session"])
        else:
            del compact["session"]
    if isinstance(compact.get("interactables"), list):
        compact["interactables"] = [compact_interactable(item) for item in compact["interactables"]]
    return compact


def compact_action_result(result: Any) -> Any:
    """What the action did and where it left the page.

    ``before`` is dropped: ``verification`` already reports what changed, and
    the pre-action page is the observation the caller acted on. ``after`` stays,
    compacted, without a second copy of the session reference.
    """
    if not isinstance(result, dict) or not isinstance(result.get("after"), dict):
        return compact_nested_session(result)
    compact = {key: value for key, value in result.items() if key != "before"}
    if "session" in compact:
        compact["session"] = compact_session(compact["session"])
    compact["after"] = compact_observation(result["after"], keep_session=False)
    return compact


def compact_nested_session(result: Any) -> Any:
    if isinstance(result, dict) and is_session_summary(result.get("session")):
        return {**result, "session": compact_session(result["session"])}
    return result


def shape_mcp_result(tool_name: str, result: Any, *, detail: ResultDetail = "compact") -> Any:
    """The MCP view of a tool result.

    Top-level session records — ``browser.create_session`` and
    ``browser.get_session`` — are returned whole: there the record is the
    answer. Everywhere else a session is a reference.
    """
    if detail == "full":
        return result
    if tool_name == "browser.observe":
        return compact_observation(result)
    if tool_name == "browser.execute_action":
        return compact_action_result(result)
    if tool_name == "browser.execute_approval" and isinstance(result, dict) and "execution" in result:
        return {**result, "execution": compact_action_result(result["execution"])}
    if tool_name == "browser.list_sessions" and isinstance(result, list):
        return [compact_session(item) for item in result]
    return compact_nested_session(result)


def inline_screenshot_path(tool_name: str, result: Any) -> str | None:
    """The screenshot a result should also carry as image content, if any.

    ``browser.screenshot`` exists to show the page, and observe's ``fast``
    preset is the screenshot-only view for vision models. Both returned only a
    path on the controller's disk and a URL on the controller, neither of which
    a model behind an MCP client can open. Other results keep their screenshot
    as a URL: an image costs more context than the text it accompanies, and the
    other presets are for reading.
    """
    if not isinstance(result, dict):
        return None
    if tool_name == "browser.screenshot" or (tool_name == "browser.observe" and result.get("preset") == "fast"):
        path = result.get("screenshot_path")
        return path if isinstance(path, str) and path else None
    return None
