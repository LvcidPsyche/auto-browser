from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    browser_cdp_endpoint: str = Field("http://browser-node:9222", alias="BROWSER_CDP_ENDPOINT")
    browser_cdp_ws_endpoint_file: str = Field(
        "/data/browser-profile/browser-ws-endpoint.txt",
        alias="BROWSER_CDP_WS_ENDPOINT_FILE",
    )
    takeover_url: str = Field(
        "http://localhost:6080/vnc.html?autoconnect=true&resize=scale",
        alias="TAKEOVER_URL",
    )
    artifact_root: str = Field("/data/artifacts", alias="ARTIFACT_ROOT")
    upload_root: str = Field("/data/uploads", alias="UPLOAD_ROOT")
    auth_root: str = Field("/data/auth", alias="AUTH_ROOT")
    allowed_hosts: str = Field("example.com,localhost", alias="ALLOWED_HOSTS")
    default_viewport_width: int = Field(1600, alias="DEFAULT_VIEWPORT_WIDTH")
    default_viewport_height: int = Field(900, alias="DEFAULT_VIEWPORT_HEIGHT")
    connect_retries: int = Field(60, alias="CONNECT_RETRIES")
    connect_retry_delay_seconds: float = Field(1.0, alias="CONNECT_RETRY_DELAY_SECONDS")
    max_sessions: int = Field(1, alias="MAX_SESSIONS")
    require_approval_for_uploads: bool = Field(True, alias="REQUIRE_APPROVAL_FOR_UPLOADS")
    enable_tracing: bool = Field(True, alias="ENABLE_TRACING")
    typing_delay_ms: int = Field(20, alias="TYPING_DELAY_MS")
    action_timeout_ms: int = Field(15000, alias="ACTION_TIMEOUT_MS")

    openai_api_key: str | None = Field(None, alias="OPENAI_API_KEY")
    openai_base_url: str = Field("https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    openai_model: str = Field("gpt-4.1-mini", alias="OPENAI_MODEL")

    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")
    anthropic_base_url: str = Field("https://api.anthropic.com/v1", alias="ANTHROPIC_BASE_URL")
    anthropic_version: str = Field("2023-06-01", alias="ANTHROPIC_VERSION")
    claude_model: str = Field("claude-sonnet-4-20250514", alias="CLAUDE_MODEL")

    gemini_api_key: str | None = Field(None, alias="GEMINI_API_KEY")
    gemini_base_url: str = Field(
        "https://generativelanguage.googleapis.com/v1beta",
        alias="GEMINI_BASE_URL",
    )
    gemini_model: str = Field("gemini-2.5-flash", alias="GEMINI_MODEL")

    model_request_timeout_seconds: float = Field(60.0, alias="MODEL_REQUEST_TIMEOUT_SECONDS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def allowed_host_patterns(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
