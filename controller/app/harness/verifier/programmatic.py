from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from ..contracts import ForbiddenState, Postcondition, TaskContract
from .base import VerificationResult

# The caller supplies the pattern, and Python's regex engine cannot be
# interrupted: it holds the GIL for the whole match, so neither wait_for nor a
# worker thread stops a catastrophic pattern ((a+)+$ on 24 characters stalled
# the event loop for 2.7 s, in a thread too). A child process can be killed.
#
# One child tests every pattern of a verification in order and prints one result
# line per pattern as it finishes, so the parent can tell which pattern is stuck.
_REGEX_WORKER = (
    "import json, re, sys\n"
    "request = json.load(sys.stdin)\n"
    "for pattern in request['patterns']:\n"
    "    sys.stdout.write('1\\n' if re.search(pattern, request['text']) else '0\\n')\n"
    "    sys.stdout.flush()\n"
)


class ProgrammaticVerifier:
    backend = "programmatic"

    def __init__(self, regex_timeout_seconds: float = 2.0) -> None:
        self.regex_timeout_seconds = regex_timeout_seconds

    async def verify(self, contract: TaskContract, trace: Any) -> VerificationResult:
        observation = _final_observation(trace)
        url = str(observation.get("url") or observation.get("current_url") or "")
        searchable_text, dom_text = _searchable_text(observation)
        evidence = _evidence_kinds(trace)

        # Every url_matches pattern (postconditions first, then forbidden states) is
        # tested in one child process; the loops below take the results in that order.
        patterns = [c.value for c in contract.postconditions if c.kind == "url_matches"]
        patterns += [f.value for f in contract.forbidden_states if f.kind == "url_matches"]
        matches = iter(await _regex_search_many(patterns, url, timeout=self.regex_timeout_seconds))

        failed: list[str] = []
        satisfied: list[str] = []
        regex_timeouts: list[str] = []
        for index, postcondition in enumerate(contract.postconditions):
            key = _condition_key(index, postcondition)
            if postcondition.kind == "url_matches":
                matched = next(matches)
                if matched is None:
                    regex_timeouts.append(key)
                passes = bool(matched)
            else:
                passes = _postcondition_passes(
                    postcondition,
                    url=url,
                    searchable_text=searchable_text,
                    dom_text=dom_text,
                    trace=trace,
                )
            if passes:
                satisfied.append(key)
            elif postcondition.required:
                failed.append(key)

        forbidden_hits: list[str] = []
        for index, forbidden in enumerate(contract.forbidden_states):
            hit_label = f"forbidden[{index}]:{forbidden.kind}:{forbidden.value or forbidden.kind}"
            if forbidden.kind == "url_matches":
                matched = next(matches)
                if matched is None:
                    # Unknown is not "clear": a forbidden state that could not be
                    # ruled out counts as hit.
                    regex_timeouts.append(hit_label)
                hit = matched is not False
            else:
                hit = _forbidden_state_hit(
                    forbidden,
                    observation=observation,
                    url=url,
                    searchable_text=searchable_text,
                    dom_text=dom_text,
                )
            if hit:
                forbidden_hits.append(hit_label)

        missing_evidence = sorted(contract.required_evidence_kinds - evidence)
        passed = not failed and not forbidden_hits and not missing_evidence
        confidence = 1.0 if passed else 0.0
        notes = "Programmatic postconditions passed." if passed else "Programmatic verification failed."
        if regex_timeouts:
            notes += f" A url_matches pattern did not finish within {self.regex_timeout_seconds:g}s."
        return VerificationResult(
            passed=passed,
            confidence=confidence,
            failed_postconditions=failed,
            satisfied_postconditions=satisfied,
            forbidden_state_hits=forbidden_hits,
            missing_evidence=missing_evidence,
            notes=notes,
            backend=self.backend,
            details={"url": url, "evidence": sorted(evidence), "regex_timeouts": regex_timeouts},
        )


def _condition_key(index: int, condition: Postcondition) -> str:
    return f"postcondition[{index}]:{condition.kind}:{condition.value}"


def _final_observation(trace: Any) -> dict[str, Any]:
    if isinstance(trace, dict):
        observation = trace.get("final_observation") or trace.get("observation") or {}
        return observation if isinstance(observation, dict) else {}

    observation = getattr(trace, "final_observation", None)
    if isinstance(observation, dict):
        return observation

    model_dump = getattr(trace, "model_dump", None)
    if callable(model_dump):
        payload = model_dump()
        observation = payload.get("final_observation") or payload.get("observation") or {}
        return observation if isinstance(observation, dict) else {}

    return {}


def _evidence_kinds(trace: Any) -> set[str]:
    raw = None
    if isinstance(trace, dict):
        raw = trace.get("evidence") or trace.get("evidence_kinds") or []
    else:
        raw = getattr(trace, "evidence", None)
        if raw is None:
            raw = getattr(trace, "evidence_kinds", None)
            if callable(raw):
                raw = raw()

    if isinstance(raw, set):
        return {str(item) for item in raw}
    if isinstance(raw, (list, tuple)):
        return {str(item) for item in raw}
    return set()


async def _regex_search_many(patterns: list[str], text: str, *, timeout: float) -> list[bool | None]:
    """re.search(pattern, text) for each pattern, in as few child processes as possible.

    One child tests the patterns in order. A pattern that does not finish within
    `timeout` of the previous result (of the child's start, for the first) gets
    None and its child is killed; the patterns after it run in a fresh child, so
    each stuck pattern costs one timeout and the others keep their own verdicts.
    """
    results: list[bool | None] = []
    remaining = patterns
    while remaining:
        finished, stuck = await _regex_search_child(remaining, text, timeout=timeout)
        results.extend(finished)
        remaining = remaining[len(finished) :]
        if stuck:
            results.append(None)
            remaining = remaining[1:]
    return results


async def _regex_search_child(patterns: list[str], text: str, *, timeout: float) -> tuple[list[bool], bool]:
    """Results for the patterns one child finished, and whether the next one outlasted `timeout`."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        _REGEX_WORKER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    finished: list[bool] = []
    try:
        process.stdin.write(json.dumps({"patterns": patterns, "text": text}).encode("utf-8"))
        process.stdin.close()
        while len(finished) < len(patterns):
            line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
            if not line:
                break  # the child exited early; the return code below says why
            finished.append(line.strip() == b"1")  # strip: Windows text mode ends lines with \r\n
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except BaseException as exc:
        # A timeout, or the caller giving up (the harness's wall-clock wait_for,
        # shutdown): either way the child must not outlive the call.
        try:
            process.kill()
        except ProcessLookupError:
            pass  # exited between the wait ending and the kill
        await process.communicate()
        if isinstance(exc, asyncio.TimeoutError):
            # Stuck only if a pattern was still unanswered (not merely slow to exit).
            return finished, len(finished) < len(patterns)
        raise
    if process.returncode != 0:
        # Patterns are compiled when the contract is built, so this is not a bad
        # pattern; fail loudly rather than guess a verdict.
        stderr = await process.stderr.read()
        raise RuntimeError(
            f"regex worker failed (code {process.returncode}): {stderr.decode('utf-8', 'replace')[-500:]}"
        )
    return finished, False


def _searchable_text(observation: dict[str, Any]) -> tuple[str, str]:
    text_fields = ("text", "body_text", "content", "title", "active_element")
    dom_fields = ("dom", "dom_outline", "accessibility_outline", "text", "ocr", "interactables")

    text_parts: list[str] = [str(value) for key in text_fields if (value := observation.get(key)) is not None]
    dom_parts: list[str] = [
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        for key in dom_fields
        if (value := observation.get(key)) is not None
    ]

    searchable_text = "\n".join(text_parts)
    dom_text = "\n".join(dom_parts)
    return searchable_text, dom_text


def _postcondition_passes(
    condition: Postcondition,
    *,
    url: str,
    searchable_text: str,
    dom_text: str,
    trace: Any,
) -> bool:
    value = condition.value
    if condition.kind == "url_contains":
        return value.lower() in url.lower()
    if condition.kind == "text_contains":
        return value.lower() in searchable_text.lower()
    if condition.kind == "dom_contains":
        return value.lower() in dom_text.lower()
    if condition.kind == "network_response_shape":
        network_entries = _network_entries(trace)
        return any(
            value.lower() in json.dumps(entry, ensure_ascii=False, default=str).lower() for entry in network_entries
        )
    if condition.kind == "extracted_data_schema":
        extracted = _extracted_data(trace)
        return value in extracted or value.lower() in json.dumps(extracted, ensure_ascii=False, default=str).lower()
    return False


def _forbidden_state_hit(
    forbidden: ForbiddenState,
    *,
    observation: dict[str, Any],
    url: str,
    searchable_text: str,
    dom_text: str,
) -> bool:
    value = forbidden.value
    if forbidden.kind == "url_contains":
        return value.lower() in url.lower()
    if forbidden.kind == "text_contains":
        target = searchable_text.lower()
        text_hit = value.lower() in target
        if text_hit:
            return True
        return value.lower() in dom_text.lower()
    if forbidden.kind == "url_status":
        status_values = {
            str(observation.get("status_code") or ""),
            str(observation.get("http_status") or ""),
            str(observation.get("status") or ""),
        }
        return (
            value.lower() in searchable_text.lower()
            or value.lower() in url.lower()
            or value.lower() in {item.lower() for item in status_values}
        )
    combined = f"{url}\n{searchable_text}\n{dom_text}".lower()
    if forbidden.kind == "captcha_screen":
        needles = ["captcha", "recaptcha", "hcaptcha", "verify you are human", "cloudflare"]
        if value:
            needles.append(value.lower())
        return any(needle in combined for needle in needles)
    if forbidden.kind == "payment_screen":
        needles = ["payment", "credit card", "card number", "billing address", "checkout payment"]
        if value:
            needles.append(value.lower())
        return any(needle in combined for needle in needles)
    if forbidden.kind == "login_redirect":
        needles = ["/login", "/signin", "sign in", "log in", "login required", "authenticate"]
        if value:
            needles.append(value.lower())
        return any(needle in combined for needle in needles)
    return False


def _network_entries(trace: Any) -> list[Any]:
    if isinstance(trace, dict):
        entries = trace.get("network") or trace.get("network_entries") or []
        return entries if isinstance(entries, list) else []
    entries = getattr(trace, "network_entries", None) or getattr(trace, "network", None) or []
    return entries if isinstance(entries, list) else []


def _extracted_data(trace: Any) -> dict[str, Any]:
    if isinstance(trace, dict):
        data = trace.get("extracted_data") or {}
        return data if isinstance(data, dict) else {}
    data = getattr(trace, "extracted_data", None) or {}
    return data if isinstance(data, dict) else {}
