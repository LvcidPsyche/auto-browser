from __future__ import annotations

import base64
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
        async with httpx.AsyncClient(timeout=timeout or self.settings.model_request_timeout_seconds) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()

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
