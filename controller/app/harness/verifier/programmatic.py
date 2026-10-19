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
_REGEX_WORKER = (
    "import json, re, sys\n"
    "request = json.load(sys.stdin)\n"
    "sys.stdout.write('1' if re.search(request['pattern'], request['text']) else '0')\n"
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

        failed: list[str] = []
        satisfied: list[str] = []
        regex_timeouts: list[str] = []
        for index, postcondition in enumerate(contract.postconditions):
            key = _condition_key(index, postcondition)
            if postcondition.kind == "url_matches":
                matched = await _regex_search(postcondition.value, url, timeout=self.regex_timeout_seconds)
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
                matched = await _regex_search(forbidden.value, url, timeout=self.regex_timeout_seconds)
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


async def _regex_search(pattern: str, text: str, *, timeout: float) -> bool | None:
    """re.search(pattern, text) in a child process; None when it outlasts `timeout`."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        _REGEX_WORKER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps({"pattern": pattern, "text": text}).encode("utf-8")),
            timeout=timeout,
        )
    except BaseException as exc:
        # A timeout, or the caller giving up (the harness's wall-clock wait_for,
        # shutdown): either way the child must not outlive the call.
        try:
            process.kill()
        except ProcessLookupError:
            pass  # exited between the wait ending and the kill
        await process.communicate()
        if isinstance(exc, asyncio.TimeoutError):
            return None
        raise
    if process.returncode != 0:
        # Patterns are compiled when the contract is built, so this is not a bad
        # pattern; fail loudly rather than guess a verdict.
        raise RuntimeError(
            f"regex worker failed (code {process.returncode}): {stderr.decode('utf-8', 'replace')[-500:]}"
        )
    return stdout == b"1"


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
