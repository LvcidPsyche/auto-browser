from __future__ import annotations

from .config import Settings
from .models import ProviderInfo, ProviderName
from .providers import ClaudeAdapter, GeminiAdapter, MinimaxAdapter, OpenAIAdapter


class ProviderRegistry:
    def __init__(self, settings: Settings):
        self.providers = {
            "openai": OpenAIAdapter(settings),
            "claude": ClaudeAdapter(settings),
            "gemini": GeminiAdapter(settings),
            "minimax": MinimaxAdapter(settings),
        }

    def get(self, name: ProviderName):
        return self.providers[name]

    def list(self) -> list[ProviderInfo]:
        infos: list[ProviderInfo] = []
        for name, adapter in self.providers.items():
            configured = adapter.configured
            infos.append(
                ProviderInfo(
                    provider=name,  # type: ignore[arg-type]
                    configured=configured,
                    model=adapter.default_model,
                    auth_mode=adapter.auth_mode,
                    detail=adapter.readiness_detail,
                    login_command=adapter.login_command,
                )
            )
        return infos
