from __future__ import annotations

import json
from typing import Any

from .base import BaseProviderAdapter, ProviderDecision
from ..models import BrowserActionDecision


class ClaudeAdapter(BaseProviderAdapter):
    provider = "claude"

    @property
    def default_model(self) -> str:
        return self.settings.claude_model

    @property
    def configured(self) -> bool:
        return bool(self.settings.anthropic_api_key)

    @property
    def missing_detail(self) -> str:
        return "ANTHROPIC_API_KEY is not configured"

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
            "max_tokens": 1024,
            "system": (
                "You are the Auto Browser planner. Pick exactly one next action. "
                "Return it via the browser_action tool."
            ),
            "tool_choice": {"type": "tool", "name": "browser_action"},
            "tools": [
                {
                    "name": "browser_action",
                    "description": "Select the single best next browser action.",
                    "input_schema": self.action_schema,
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": self.build_text_prompt(
                                goal=goal,
                                observation=observation,
                                context_hints=context_hints,
                                previous_steps=previous_steps,
                            ),
                        },
                    ],
                }
            ],
        }
        response = await self._post_json(
            url=f"{self.settings.anthropic_base_url.rstrip('/')}/messages",
            headers={
                "x-api-key": self.settings.anthropic_api_key or "",
                "anthropic-version": self.settings.anthropic_version,
                "content-type": "application/json",
            },
            payload=payload,
        )
        tool_use = next((item for item in response.get("content", []) if item.get("type") == "tool_use"), None)
        if not tool_use:
            raise RuntimeError("Claude did not return a browser_action tool_use block")
        decision = BrowserActionDecision.model_validate(tool_use.get("input", {}))
        usage = response.get("usage")
        return ProviderDecision(
            provider=self.provider,
            model=response.get("model", model),
            decision=decision,
            usage=usage,
            raw_text=json.dumps(tool_use.get("input", {}), ensure_ascii=False),
        )
