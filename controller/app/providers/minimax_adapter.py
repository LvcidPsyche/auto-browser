from __future__ import annotations

from typing import Any

from ..models import BrowserActionDecision
from .base import BaseProviderAdapter, ProviderDecision


class MinimaxAdapter(BaseProviderAdapter):
    provider = "minimax"

    @property
    def supported_auth_modes(self) -> tuple[str, ...]:
        return ("api",)

    @property
    def default_model(self) -> str:
        return self.settings.minimax_model

    @property
    def configured(self) -> bool:
        ready, _ = self._readiness()
        return ready

    @property
    def missing_detail(self) -> str:
        return self.readiness_detail

    @property
    def readiness_detail(self) -> str:
        _, detail = self._readiness()
        return detail

    @property
    def auth_mode(self) -> str:
        return self.normalize_auth_mode(self.settings.minimax_auth_mode)

    def _readiness(self) -> tuple[bool, str]:
        if not self.auth_mode_supported(self.auth_mode):
            return False, self.invalid_auth_mode_detail(self.auth_mode)
        return self.describe_api_readiness(api_key=self.settings.minimax_api_key, env_var="MINIMAX_API_KEY")

    async def _decide(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        model_override: str | None,
    ) -> ProviderDecision:
        model = model_override or self.settings.minimax_model
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
            url=f"{self.settings.minimax_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.settings.minimax_api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
        )
        choice = response["choices"][0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            raise RuntimeError("MiniMax did not return a tool call for browser_action")
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
