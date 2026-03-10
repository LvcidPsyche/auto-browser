from __future__ import annotations

import json
from typing import Any

from .base import BaseProviderAdapter, ProviderDecision
from ..models import BrowserActionDecision


class OpenAIAdapter(BaseProviderAdapter):
    provider = "openai"

    @property
    def default_model(self) -> str:
        return self.settings.openai_model

    @property
    def configured(self) -> bool:
        return bool(self.settings.openai_api_key)

    @property
    def missing_detail(self) -> str:
        return "OPENAI_API_KEY is not configured"

    async def _decide(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        model_override: str | None,
    ) -> ProviderDecision:
        model = model_override or self.default_model
        mime_type, image_b64 = self.encode_image(observation["screenshot_path"])
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are the Auto Browser planner. Pick exactly one next action. "
                        "Use the provided function tool for your answer."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": self.build_text_prompt(
                                goal=goal,
                                observation=observation,
                                context_hints=context_hints,
                                previous_steps=previous_steps,
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_b64}",
                            },
                        },
                    ],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "browser_action",
                        "description": "Select the single best next browser action.",
                        "parameters": self.action_schema,
                        "strict": True,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "browser_action"}},
        }
        response = await self._post_json(
            url=f"{self.settings.openai_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
        )
        choice = response["choices"][0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            raise RuntimeError("OpenAI did not return a tool call for browser_action")
        arguments = tool_calls[0]["function"]["arguments"]
        decision = BrowserActionDecision.model_validate_json(arguments)
        usage = response.get("usage")
        return ProviderDecision(
            provider=self.provider,
            model=response.get("model", model),
            decision=decision,
            usage=usage,
            raw_text=arguments,
        )
