from __future__ import annotations

import json
from typing import Any

from ..models import BrowserActionDecision
from .base import BaseProviderAdapter, ProviderDecision


class ClaudeAdapter(BaseProviderAdapter):
    provider = "claude"

    @property
    def default_model(self) -> str:
        return self.settings.claude_cli_model or self.settings.claude_model

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
        return self.normalize_auth_mode(self.settings.claude_auth_mode)

    @property
    def login_command(self) -> str | None:
        if self.auth_mode == "cli":
            return self.settings.claude_cli_path or "claude"
        return None

    def _readiness(self) -> tuple[bool, str]:
        if not self.auth_mode_supported(self.auth_mode):
            return False, self.invalid_auth_mode_detail(self.auth_mode)
        if self.auth_mode == "cli":
            return self.describe_cli_readiness(
                cli_path=self.settings.claude_cli_path,
                cli_label="claude",
                auth_markers=(".claude.json", ".claude"),
            )
        return self.describe_api_readiness(api_key=self.settings.anthropic_api_key, env_var="ANTHROPIC_API_KEY")

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

        model = model_override or self.settings.claude_model
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

    async def _decide_via_cli(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        model_override: str | None,
    ) -> ProviderDecision:
        model = model_override or self.settings.claude_cli_model
        prompt = self.build_cli_prompt(
            goal=goal,
            observation=observation,
            context_hints=context_hints,
            previous_steps=previous_steps,
        )
        command = [
            self.settings.claude_cli_path,
            "--print",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(self.action_schema, ensure_ascii=False),
            "--permission-mode",
            "plan",
            "--tools",
            "",
            "--no-session-persistence",
            prompt,
        ]
        if model:
            command[1:1] = ["--model", model]

        result = await self.run_cli(command=command)
        decision = self.parse_decision_text(result.stdout)
        return ProviderDecision(
            provider=self.provider,
            model=model or self.default_model,
            decision=decision,
            usage={"auth_mode": "cli", "transport": "claude-cli"},
            raw_text=result.stdout,
        )
