from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .base import BaseProviderAdapter, ProviderDecision
from ..models import BrowserActionDecision


class OpenAIAdapter(BaseProviderAdapter):
    provider = "openai"

    @property
    def default_model(self) -> str:
        return self.settings.openai_cli_model or self.settings.openai_model

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
        return self.normalize_auth_mode(self.settings.openai_auth_mode)

    def _readiness(self) -> tuple[bool, str]:
        if not self.auth_mode_supported(self.auth_mode):
            return False, self.invalid_auth_mode_detail(self.auth_mode)
        if self.auth_mode == "cli":
            return self.describe_cli_readiness(
                cli_path=self.settings.openai_cli_path,
                cli_label="codex",
                auth_markers=(".codex",),
            )
        return self.describe_api_readiness(api_key=self.settings.openai_api_key, env_var="OPENAI_API_KEY")

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

        model = model_override or self.settings.openai_model
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

    async def _decide_via_cli(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        model_override: str | None,
    ) -> ProviderDecision:
        model = model_override or self.settings.openai_cli_model
        prompt = self.build_cli_prompt(
            goal=goal,
            observation=observation,
            context_hints=context_hints,
            previous_steps=previous_steps,
            include_schema=False,
        )
        with tempfile.TemporaryDirectory() as tempdir:
            temp_root = Path(tempdir)
            schema_path = temp_root / "browser_action_schema.json"
            output_path = temp_root / "decision.json"
            schema_path.write_text(json.dumps(self.action_schema, ensure_ascii=False), encoding="utf-8")

            command = [
                self.settings.openai_cli_path,
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--cd",
                tempdir,
                "--ephemeral",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--image",
                observation["screenshot_path"],
                "-",
            ]
            if model:
                command[1:1] = ["--model", model]

            result = await self.run_cli(command=command, input_text=prompt, cwd=tempdir)
            raw_text = output_path.read_text(encoding="utf-8") if output_path.exists() else result.stdout
            decision = self.parse_decision_text(raw_text)
            return ProviderDecision(
                provider=self.provider,
                model=model or self.default_model,
                decision=decision,
                usage={"auth_mode": "cli", "transport": "codex-exec"},
                raw_text=raw_text,
            )
