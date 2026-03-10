from __future__ import annotations

import json
from typing import Any

from .base import BaseProviderAdapter, ProviderDecision
from ..models import BrowserActionDecision


class GeminiAdapter(BaseProviderAdapter):
    provider = "gemini"

    @property
    def default_model(self) -> str:
        return self.settings.gemini_cli_model or self.settings.gemini_model

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
        return self.normalize_auth_mode(self.settings.gemini_auth_mode)

    def _readiness(self) -> tuple[bool, str]:
        if not self.auth_mode_supported(self.auth_mode):
            return False, self.invalid_auth_mode_detail(self.auth_mode)
        if self.auth_mode == "cli":
            return self.describe_cli_readiness(
                cli_path=self.settings.gemini_cli_path,
                cli_label="gemini",
                auth_markers=(".gemini",),
            )
        return self.describe_api_readiness(api_key=self.settings.gemini_api_key, env_var="GEMINI_API_KEY")

    async def _decide(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        model_override: str | None,
    ) -> ProviderDecision:
        if self.auth_mode == "cli":
            return await self._decide_via_cli(
                goal=goal,
                observation=observation,
                context_hints=context_hints,
                previous_steps=previous_steps,
                model_override=model_override,
            )

        model = model_override or self.settings.gemini_model
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

    async def _decide_via_cli(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        model_override: str | None,
    ) -> ProviderDecision:
        model = model_override or self.settings.gemini_cli_model
        prompt = self.build_cli_prompt(
            goal=goal,
            observation=observation,
            context_hints=context_hints,
            previous_steps=previous_steps,
        )
        command = [
            self.settings.gemini_cli_path,
            "--prompt",
            prompt,
            "--output-format",
            "json",
            "--approval-mode",
            "plan",
        ]
        if model:
            command.extend(["--model", model])

        result = await self.run_cli(command=command)
        decision = self.parse_decision_text(result.stdout)
        return ProviderDecision(
            provider=self.provider,
            model=model or self.default_model,
            decision=decision,
            usage={"auth_mode": "cli", "transport": "gemini-cli"},
            raw_text=result.stdout,
        )
