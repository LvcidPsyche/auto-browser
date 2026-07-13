"""Generic adapter for any OpenAI /chat/completions-compatible endpoint.

A single parameterized adapter backs every provider that speaks the OpenAI
chat-completions dialect, so Auto Browser is not limited to the three first-class
providers. In particular this covers:

- **OpenRouter** — one key proxies essentially every frontier model (Claude, GPT,
  Gemini, Grok, DeepSeek, Llama, Mistral, Qwen, ...), so "all models" is one config away.
- **xAI (Grok)**, **DeepSeek**, **MiniMax** — popular direct endpoints.
- **openai_compatible** — a fully custom base URL for anything else: a self-hosted
  Ollama / vLLM / LM Studio server, Azure OpenAI, Together, Groq, Fireworks, etc.

Vision + function-calling, same request shape as the OpenAI adapter's API path, with a
content-parsing fallback for endpoints that don't honor tool_choice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..models import BrowserActionDecision, ProviderName
from .base import BaseProviderAdapter, ProviderDecision


@dataclass(frozen=True)
class OpenAICompatibleProfile:
    """Static description of one OpenAI-compatible provider.

    The three ``*_attr`` fields name the ``Settings`` attributes that hold this
    provider's API key, base URL, and model, so one adapter class can serve many
    providers by reading different config slots.
    """

    provider: str
    label: str
    api_key_attr: str
    base_url_attr: str
    model_attr: str
    env_var: str
    supports_vision: bool = True


# base_url defaults are stable; model defaults are current-but-overridable via env.
# Empty model/base_url means "the operator must set it" (surfaced by readiness below).
OPENAI_COMPATIBLE_PROFILES: tuple[OpenAICompatibleProfile, ...] = (
    OpenAICompatibleProfile(
        provider="openrouter",
        label="OpenRouter",
        api_key_attr="openrouter_api_key",
        base_url_attr="openrouter_base_url",
        model_attr="openrouter_model",
        env_var="OPENROUTER_API_KEY",
        supports_vision=True,
    ),
    OpenAICompatibleProfile(
        provider="xai",
        label="xAI Grok",
        api_key_attr="xai_api_key",
        base_url_attr="xai_base_url",
        model_attr="xai_model",
        env_var="XAI_API_KEY",
        supports_vision=True,
    ),
    OpenAICompatibleProfile(
        provider="deepseek",
        label="DeepSeek",
        api_key_attr="deepseek_api_key",
        base_url_attr="deepseek_base_url",
        model_attr="deepseek_model",
        env_var="DEEPSEEK_API_KEY",
        # deepseek-chat is text-only; drive it from the DOM/accessibility outline.
        supports_vision=False,
    ),
    OpenAICompatibleProfile(
        provider="minimax",
        label="MiniMax",
        api_key_attr="minimax_api_key",
        base_url_attr="minimax_base_url",
        model_attr="minimax_model",
        env_var="MINIMAX_API_KEY",
        supports_vision=True,
    ),
    OpenAICompatibleProfile(
        provider="openai_compatible",
        label="OpenAI-compatible",
        api_key_attr="openai_compatible_api_key",
        base_url_attr="openai_compatible_base_url",
        model_attr="openai_compatible_model",
        env_var="OPENAI_COMPATIBLE_API_KEY",
        supports_vision=True,
    ),
)


class OpenAICompatibleAdapter(BaseProviderAdapter):
    """Adapter for a single OpenAI-compatible endpoint described by a profile."""

    def __init__(self, settings: Settings, profile: OpenAICompatibleProfile):
        super().__init__(settings)
        # Instance attribute overrides the class-level ``provider`` annotation so one
        # class can present as many providers.
        self.provider = profile.provider  # type: ignore[assignment]
        self.profile = profile

    def _cfg(self, attr: str) -> Any:
        return getattr(self.settings, attr)

    @property
    def supported_auth_modes(self) -> tuple[str, ...]:
        return ("api",)

    @property
    def auth_mode(self) -> str:
        return "api"

    @property
    def default_model(self) -> str:
        return self._cfg(self.profile.model_attr) or ""

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

    def _readiness(self) -> tuple[bool, str]:
        api_key = self._cfg(self.profile.api_key_attr)
        base_url = self._cfg(self.profile.base_url_attr)
        model = self._cfg(self.profile.model_attr)
        missing = []
        if not api_key:
            missing.append(self.profile.env_var)
        if not base_url:
            missing.append(self.profile.env_var.replace("_API_KEY", "_BASE_URL"))
        if not model:
            missing.append(self.profile.env_var.replace("_API_KEY", "_MODEL"))
        if missing:
            return False, f"{self.profile.label} not configured; set: {', '.join(missing)}"
        return True, f"ready via {self.profile.env_var}"

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
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": self.build_text_prompt(
                    goal=goal,
                    observation=observation,
                    context_hints=context_hints,
                    previous_steps=previous_steps,
                ),
            }
        ]
        if self.profile.supports_vision:
            mime_type, image_b64 = self.encode_image(observation["screenshot_path"])
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}}
            )

        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are the Auto Browser planner. Pick exactly one next action. "
                        "Use the provided function tool for your answer, or if you cannot "
                        "call tools, reply with only the matching JSON object."
                    ),
                },
                {"role": "user", "content": content},
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

        base_url = str(self._cfg(self.profile.base_url_attr)).rstrip("/")
        response = await self._post_json(
            url=f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._cfg(self.profile.api_key_attr)}",
                "Content-Type": "application/json",
            },
            payload=payload,
        )

        choices = response.get("choices") or []
        if not choices:
            raise RuntimeError(f"{self.profile.label} response contained no choices")
        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            raw_text = tool_calls[0]["function"]["arguments"]
            decision = BrowserActionDecision.model_validate_json(raw_text)
        else:
            # Some OpenAI-compatible endpoints ignore tool_choice; parse the content.
            raw_text = str(message.get("content") or "").strip()
            if not raw_text:
                raise RuntimeError(
                    f"{self.profile.label} returned neither a browser_action tool call nor content"
                )
            decision = self.parse_decision_text(raw_text)

        return ProviderDecision(
            provider=self.provider,
            model=response.get("model", model),
            decision=decision,
            usage=response.get("usage"),
            raw_text=raw_text,
        )


def build_openai_compatible_adapters(settings: Settings) -> dict[ProviderName, OpenAICompatibleAdapter]:
    """One adapter per profile, keyed by provider name for the registry."""
    return {
        profile.provider: OpenAICompatibleAdapter(settings, profile)  # type: ignore[misc]
        for profile in OPENAI_COMPATIBLE_PROFILES
    }
