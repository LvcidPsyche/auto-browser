from __future__ import annotations

import base64
import asyncio
import json
import mimetypes
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..models import BROWSER_ACTION_SCHEMA, BrowserActionDecision, ProviderName


@dataclass
class ProviderDecision:
    provider: ProviderName
    model: str
    decision: BrowserActionDecision
    usage: dict[str, Any] | None = None
    raw_text: str | None = None


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
