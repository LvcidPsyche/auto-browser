from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from typing import Any
from urllib.parse import urlsplit

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
        # A contract whose postconditions are all optional has nothing that must hold, so passing it
        # takes at least one of them being satisfied; otherwise it would pass on a page that matches none.
        nothing_satisfied = not satisfied and not any(c.required for c in contract.postconditions)
        passed = not failed and not forbidden_hits and not missing_evidence and not nothing_satisfied
        confidence = 1.0 if passed else 0.0
        notes = "Programmatic postconditions passed." if passed else "Programmatic verification failed."
        if nothing_satisfied:
            notes += " Every postcondition is optional and none was satisfied."
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

    Cost, measured 2026-11-05 with Python 3.12 on two Windows 11 laptops (100 calls with one pattern each): the
    price is the interpreter spawn, about the same for one pattern as for 100, so it is one start per verification
    unless a pattern times out; each stuck pattern adds one (at most one verification per attempt, 20 attempts at
    most), not one per check. With `-I` alone a call took 176 ms on one laptop and 88-93 ms on the other; with
    `-I -S` (the worker needs no site module) 124 ms and 69 ms. A long-lived worker is not worth its complexity for
    that; revisit if a verification ever needs more than one spawn per attempt. Linux was not measured.
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
        "-S",  # the worker imports only json, re and sys: no site module, so a cheaper start
        "-c",
        _REGEX_WORKER,
        # It needs no variable (W2-27); Windows' Python cannot start without SYSTEMROOT.
        env={name: os.environ[name] for name in ("SYSTEMROOT",) if name in os.environ},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    finished: list[bool] = []
    # Drained alongside stdout: re warns on stderr while it compiles (a nested
    # set, say), and a child blocked on a full stderr pipe looks like a stuck pattern.
    stderr_reader = asyncio.ensure_future(process.stderr.read())
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
        await process.stdout.read()
        await stderr_reader
        await process.wait()
        if isinstance(exc, asyncio.TimeoutError):
            # Stuck only if a pattern was still unanswered (not merely slow to exit).
            return finished, len(finished) < len(patterns)
        raise
    stderr = await stderr_reader
    if process.returncode != 0:
        # Patterns are compiled when the contract is built, so this is not a bad
        # pattern; fail loudly rather than guess a verdict.
        raise RuntimeError(
            f"regex worker failed (code {process.returncode}): {stderr.decode('utf-8', 'replace')[-500:]}"
        )
    return finished, False


def _searchable_text(observation: dict[str, Any]) -> tuple[str, str]:
    # `text_excerpt` is where an observation (observation.py) carries the page text.
    text_fields = ("text", "text_excerpt", "body_text", "content", "title", "active_element")
    dom_fields = ("dom", "dom_outline", "accessibility_outline", "text", "text_excerpt", "ocr", "interactables")

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
    """Whether the observation shows the forbidden state.

    The built-in kinds are heuristics. captcha_screen and payment_screen match their phrases as whole words in the URL
    and the page text (for captcha names `_`, `-` and camelCase humps end words too); login_redirect reads where the
    browser is. Known false hits, left as they are: a page-code flag such as `captcha_enabled` counts as a captcha
    name, and an API path such as `/payments/1/authorize` as a login page (`authorize`).
    """
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
    combined = f"{url}\n{searchable_text}\n{dom_text}"
    if forbidden.kind == "captcha_screen":
        return _has_captcha_name(combined) or _has_value(combined, value)
    if forbidden.kind == "payment_screen":
        return (
            _has_phrase(combined, _PAYMENT_PHRASES)
            or _path_has_segment(url, _PAYMENT_SEGMENTS)
            or _has_value(combined, value)
        )
    if forbidden.kind == "login_redirect":
        # What the browser was sent to, not what the page says: a marketing page that offers "sign in" is not a
        # redirect. The URL's path or host, a 401/403 status, and the caller's value as part of the URL.
        status = {str(observation.get(key) or "").strip() for key in ("status_code", "http_status", "status")}
        return (
            _path_has_segment(url, _LOGIN_SEGMENTS)
            or _host_is_login(url)
            or bool(status & {"401", "403"})
            or bool(value and value.lower() in url.lower())
        )
    return False


_CAPTCHA_PHRASES = ("captcha", "recaptcha", "hcaptcha", "grecaptcha", "verify you are human", "cloudflare")
_PAYMENT_PHRASES = (
    "credit card",
    "card number",
    "billing address",
    "checkout payment",
    "payment method",
    "payment details",
    "payment information",
)
_PAYMENT_SEGMENTS = frozenset({"payment", "payments"})
_LOGIN_SEGMENTS = frozenset(
    {
        "login",
        "log-in",
        "log_in",
        "logon",
        "signin",
        "sign-in",
        "sign_in",
        "auth",
        "authorize",
        "wp-login",  # WordPress, with or without .php
        "servicelogin",  # Google accounts (ServiceLogin; paths are compared lower-cased)
        "login-actions",  # Keycloak
        "signin-oidc",  # ASP.NET Core
    }
)
_LOGIN_HOST_LABELS = _LOGIN_SEGMENTS | {"sso"}
# Where an identifier in page code changes case: captchaToken -> captcha Token, reCAPTCHAWidget -> re CAPTCHA Widget.
_CAMEL_HUMP = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _has_captcha_name(text: str) -> bool:
    """A captcha phrase as whole words, in the text as written or split at camelCase humps (`captchaToken`).

    Both, so splitting costs no hit the text as written has (`CloudFlare` is not `Cloud Flare`); `captchaless` is not
    a hit either way.
    """
    return _has_phrase(text, _CAPTCHA_PHRASES, underscore_ends_words=True) or _has_phrase(
        _CAMEL_HUMP.sub(" ", text), _CAPTCHA_PHRASES, underscore_ends_words=True
    )


def _has_phrase(text: str, phrases: list[str] | tuple[str, ...], *, underscore_ends_words: bool = False) -> bool:
    """Whether any phrase occurs in the text as whole words, ignoring case ("accredit cards" is not "credit card").

    With `underscore_ends_words`, `_` also separates words, for names in page code (`captcha_image`, `h_captcha`).
    """
    word = r"[^\W_]" if underscore_ends_words else r"\w"
    return any(
        re.search(rf"(?<!{word}){re.escape(phrase.strip())}(?!{word})", text, re.IGNORECASE)
        for phrase in phrases
        if phrase.strip()
    )


def _host_is_login(url: str) -> bool:
    """Whether the URL's host starts with a login label (`login.microsoftonline.com`, `sso.example.com`)."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return host.split(".")[0] in _LOGIN_HOST_LABELS


def _has_value(text: str, value: str) -> bool:
    """The caller's own value, as a case-insensitive substring as it always was: a path such as `/billing` must
    still match next to a letter, so the whole-word rule is for the built-in phrases only."""
    return bool(value) and value.lower() in text.lower()


def _path_has_segment(url: str, segments: frozenset[str]) -> bool:
    """Whether a path segment of the URL is one of `segments`, with or without a file extension (`SignIn.html`) or
    path parameters (`login;jsessionid=...`). A fragment that is an app's route (`#/login`, `#!/signin`) is read as a
    path too; an in-page anchor (`#login`) is not."""
    try:
        parts = urlsplit(url)
        paths = [parts.path]
        if parts.fragment.startswith(("/", "!/")):
            paths.append(urlsplit(parts.fragment.removeprefix("!")).path)
    except ValueError:
        return False
    return any(
        segment.split(";")[0].split(".")[0] in segments
        for path in paths
        for segment in path.lower().split("/")
        if segment
    )


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
