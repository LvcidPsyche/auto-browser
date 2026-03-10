from __future__ import annotations

import base64
import asyncio
import json
import mimetypes
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any

import httpx

from ..config import Settings
from ..models import BROWSER_ACTION_SCHEMA, BrowserActionDecision, ProviderName

VALID_PROVIDER_AUTH_MODES = {"api", "cli"}


@dataclass
class ProviderDecision:
    provider: ProviderName
    model: str
    decision: BrowserActionDecision
    usage: dict[str, Any] | None = None
    raw_text: str | None = None


@dataclass
class CLIResult:
    command: list[str]
    stdout: str
    stderr: str
    returncode: int


@dataclass
class ProviderAPIError(RuntimeError):
    provider: ProviderName
    message: str
    status_code: int | None = None
    retryable: bool = False
    raw_error: dict[str, Any] | None = None

    def __str__(self) -> str:
        if self.status_code is None:
            return f"{self.provider} provider error: {self.message}"
        return f"{self.provider} provider error ({self.status_code}): {self.message}"


class BaseProviderAdapter(ABC):
    provider: ProviderName

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    @abstractmethod
    def default_model(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def configured(self) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def missing_detail(self) -> str:
        raise NotImplementedError

    @property
    def readiness_detail(self) -> str:
        return "configured" if self.configured else self.missing_detail

    async def decide(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None = None,
        previous_steps: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
    ) -> ProviderDecision:
        if not self.configured:
            raise RuntimeError(self.missing_detail)
        return await self._decide(
            goal=goal,
            observation=observation,
            context_hints=context_hints,
            previous_steps=previous_steps or [],
            model_override=model_override,
        )

    @abstractmethod
    async def _decide(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        model_override: str | None,
    ) -> ProviderDecision:
        raise NotImplementedError

    async def _post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        max_attempts = max(1, self.settings.model_max_retries + 1)
        async with httpx.AsyncClient(timeout=timeout or self.settings.model_request_timeout_seconds) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    response = await client.post(url, headers=headers, json=payload)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt < max_attempts:
                        await asyncio.sleep(self.settings.model_retry_backoff_seconds * (2 ** (attempt - 1)))
                        continue
                    raise ProviderAPIError(
                        provider=self.provider,
                        message=str(exc),
                        status_code=None,
                        retryable=True,
                    ) from exc

                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < max_attempts:
                        await asyncio.sleep(self.settings.model_retry_backoff_seconds * (2 ** (attempt - 1)))
                        continue

                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    payload = self._safe_json(response)
                    raise ProviderAPIError(
                        provider=self.provider,
                        message=self._extract_error_message(payload) or response.text[:300],
                        status_code=response.status_code,
                        retryable=response.status_code == 429 or response.status_code >= 500,
                        raw_error=payload if isinstance(payload, dict) else None,
                    ) from exc

                return response.json()

        raise ProviderAPIError(provider=self.provider, message="request failed without a response")

    @staticmethod
    def encode_image(path: str) -> tuple[str, str]:
        file_path = Path(path)
        mime_type = mimetypes.guess_type(file_path.name)[0] or "image/png"
        data = base64.b64encode(file_path.read_bytes()).decode("ascii")
        return mime_type, data

    @staticmethod
    def compact_observation(observation: dict[str, Any]) -> dict[str, Any]:
        interactables = []
        for item in observation.get("interactables", []):
            interactables.append(
                {
                    "element_id": item.get("element_id"),
                    "label": item.get("label"),
                    "role": item.get("role"),
                    "tag": item.get("tag"),
                    "type": item.get("type"),
                    "disabled": item.get("disabled"),
                    "href": item.get("href"),
                    "bbox": item.get("bbox"),
                    "selector_hint": item.get("selector_hint"),
                }
            )
        return {
            "session": observation.get("session"),
            "url": observation.get("url"),
            "title": observation.get("title"),
            "active_element": observation.get("active_element"),
            "text_excerpt": observation.get("text_excerpt"),
            "dom_outline": observation.get("dom_outline"),
            "accessibility_outline": observation.get("accessibility_outline"),
            "ocr": observation.get("ocr"),
            "interactables": interactables,
            "console_messages": observation.get("console_messages", []),
            "page_errors": observation.get("page_errors", []),
            "request_failures": observation.get("request_failures", []),
            "takeover_url": observation.get("takeover_url"),
        }

    def build_text_prompt(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
    ) -> str:
        compact_observation = self.compact_observation(observation)
        prior_steps = previous_steps[-6:]
        return (
            "Choose exactly one next browser action.\n"
            "Rules:\n"
            "- Use only the current observation. element_id values are observation-scoped.\n"
            "- Prefer element_id over selector. Use coordinates only for click when no reliable locator exists.\n"
            "- Never invent URLs, elements, or file paths.\n"
            "- Always set risk_category. Use read for navigate/scroll/done, write for normal click/type/press, upload for file uploads.\n"
            "- If an action would post/send/publish content, set risk_category=post.\n"
            "- If an action would submit a payment/order, set risk_category=payment.\n"
            "- If an action would change profile/settings/security/billing/account state, set risk_category=account_change.\n"
            "- If an action would delete/remove/cancel/close something, set risk_category=destructive.\n"
            "- If the goal is already complete, return action=done.\n"
            "- If the next step involves login, MFA, CAPTCHA, payments, sending/posting, or you are uncertain, return action=request_human_takeover.\n"
            "- For upload, use only an explicitly provided staged file_path.\n"
            f"Goal:\n{goal}\n\n"
            f"Context hints:\n{context_hints or 'None'}\n\n"
            f"Previous steps (most recent last):\n{json.dumps(prior_steps, ensure_ascii=False)}\n\n"
            f"Current observation:\n{json.dumps(compact_observation, ensure_ascii=False)}"
        )

    @property
    def action_schema(self) -> dict[str, Any]:
        return BROWSER_ACTION_SCHEMA

    def build_cli_prompt(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        include_schema: bool = True,
    ) -> str:
        schema_text = ""
        if include_schema:
            schema_text = (
                "Return only a single JSON object that matches this JSON Schema. "
                "Do not wrap it in markdown.\n"
                f"{json.dumps(self.action_schema, ensure_ascii=False)}\n\n"
            )
        return (
            schema_text
            + self.build_text_prompt(
                goal=goal,
                observation=observation,
                context_hints=context_hints,
                previous_steps=previous_steps,
            )
        )

    def cli_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.settings.cli_home:
            env["HOME"] = self.settings.cli_home
        env.setdefault("CI", "1")
        env.setdefault("NO_COLOR", "1")
        return env

    @staticmethod
    def cli_binary_exists(path: str | None) -> bool:
        return bool(path and which(path))

    @staticmethod
    def normalize_auth_mode(raw: str | None) -> str:
        return (raw or "").strip().lower()

    @classmethod
    def auth_mode_supported(cls, value: str) -> bool:
        return value in VALID_PROVIDER_AUTH_MODES

    def invalid_auth_mode_detail(self, value: str) -> str:
        value_label = value or "<empty>"
        return f"{self.provider} auth mode '{value_label}' is invalid; expected one of: api, cli"

    @staticmethod
    def describe_api_readiness(*, api_key: str | None, env_var: str) -> tuple[bool, str]:
        if api_key:
            return True, f"ready via {env_var}"
        return False, f"{env_var} is not configured"

    def describe_cli_readiness(
        self,
        *,
        cli_path: str | None,
        cli_label: str,
        auth_markers: tuple[str, ...],
    ) -> tuple[bool, str]:
        resolved_cli = which(cli_path) if cli_path else None
        if not resolved_cli:
            expected_path = cli_path or cli_label
            return False, f"{self.provider} CLI binary was not found: {expected_path}"

        cli_home = (self.settings.cli_home or "").strip()
        if not cli_home:
            return True, f"ready via {cli_label} CLI ({resolved_cli}); CLI_HOME is unset so auth state is delegated to the CLI environment"

        home_path = Path(cli_home)
        if not home_path.exists():
            return False, f"CLI_HOME path does not exist: {home_path}"

        if not auth_markers:
            return True, f"ready via {cli_label} CLI ({resolved_cli}) with CLI_HOME={home_path}"

        matches = [str(home_path / marker) for marker in auth_markers if (home_path / marker).exists()]
        if not matches:
            expected = ", ".join(str(home_path / marker) for marker in auth_markers)
            return False, f"No {self.provider} CLI auth state found under {home_path}; expected one of: {expected}"

        return True, f"ready via {cli_label} CLI ({resolved_cli}); auth state found at {', '.join(matches)}"

    async def run_cli(
        self,
        *,
        command: list[str],
        input_text: str | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> CLIResult:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env or self.cli_environment(),
            cwd=cwd,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input_text.encode("utf-8") if input_text is not None else None),
                timeout=self.settings.model_request_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise ProviderAPIError(
                provider=self.provider,
                message=f"CLI command timed out after {self.settings.model_request_timeout_seconds:.0f}s",
                retryable=True,
            ) from exc

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"CLI exited with code {process.returncode}"
            raise ProviderAPIError(
                provider=self.provider,
                message=detail[:1200],
                retryable=False,
            )
        return CLIResult(command=command, stdout=stdout, stderr=stderr, returncode=process.returncode)

    def parse_decision_text(self, text: str) -> BrowserActionDecision:
        text = text.strip()
        if not text:
            raise RuntimeError("provider returned an empty response")

        try:
            return BrowserActionDecision.model_validate_json(text)
        except Exception:
            pass

        try:
            payload = json.loads(text)
        except Exception:
            payload = None

        if payload is not None:
            decision = self._find_decision_candidate(payload)
            if decision is not None:
                return decision

        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[index:])
            except Exception:
                continue
            decision = self._find_decision_candidate(candidate)
            if decision is not None:
                return decision

        raise RuntimeError(f"{self.provider} CLI did not return a valid BrowserActionDecision JSON object")

    def parse_decision_file(self, path: Path) -> BrowserActionDecision:
        return self.parse_decision_text(path.read_text(encoding="utf-8"))

    @staticmethod
    def _find_decision_candidate(payload: Any) -> BrowserActionDecision | None:
        if isinstance(payload, dict):
            try:
                return BrowserActionDecision.model_validate(payload)
            except Exception:
                pass
            for value in payload.values():
                decision = BaseProviderAdapter._find_decision_candidate(value)
                if decision is not None:
                    return decision
        elif isinstance(payload, list):
            for item in payload:
                decision = BaseProviderAdapter._find_decision_candidate(item)
                if decision is not None:
                    return decision
        return None

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any] | None:
        try:
            payload = response.json()
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _extract_error_message(payload: dict[str, Any] | None) -> str | None:
        if not payload:
            return None
        error = payload.get("error")
        if isinstance(error, dict):
            parts = [
                error.get("message"),
                error.get("type"),
                error.get("status"),
                error.get("code"),
            ]
            text = " | ".join(str(part) for part in parts if part)
            if text:
                return text
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message
        return None
