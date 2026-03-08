from __future__ import annotations

import json
from typing import Any

from .base import BaseProviderAdapter, ProviderDecision
from ..models import BrowserActionDecision


class GeminiAdapter(BaseProviderAdapter):
    provider = "gemini"

    @property
    def default_model(self) -> str:
        return self.settings.gemini_model

    @property
    def configured(self) -> bool:
        return bool(self.settings.gemini_api_key)

    @property
    def missing_detail(self) -> str:
        return "GEMINI_API_KEY is not configured"

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
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inlineData": {"mimeType": mime_type, "data": image_b64}},
                        {
                            "text": self.build_text_prompt(
                                goal=goal,
                                observation=observation,
                                context_hints=context_hints,
                                previous_steps=previous_steps,
                            )
                        },
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseJsonSchema": self.action_schema,
            },
        }
        response = await self._post_json(
            url=f"{self.settings.gemini_base_url.rstrip('/')}/models/{model}:generateContent",
            headers={
                "x-goog-api-key": self.settings.gemini_api_key or "",
                "content-type": "application/json",
            },
            payload=payload,
        )
        candidates = response.get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini returned no candidates")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = next((part.get("text") for part in parts if part.get("text")), None)
        if not text:
            raise RuntimeError("Gemini did not return structured JSON text")
        decision = BrowserActionDecision.model_validate_json(text)
        usage = response.get("usageMetadata")
        return ProviderDecision(
            provider=self.provider,
            model=model,
            decision=decision,
            usage=usage,
            raw_text=text,
        )
