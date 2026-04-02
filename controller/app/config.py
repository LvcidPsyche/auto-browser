import random
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = Field("development", validation_alias=AliasChoices("APP_ENV", "ENVIRONMENT"))
    api_bearer_token: str | None = Field(None, alias="API_BEARER_TOKEN")
    browser_ws_endpoint: str | None = Field(
        None,
        validation_alias=AliasChoices("BROWSER_WS_ENDPOINT", "BROWSER_CDP_ENDPOINT"),
    )
    browser_ws_endpoint_file: str = Field(
        "/data/browser-profile/browser-ws-endpoint.txt",
        validation_alias=AliasChoices("BROWSER_WS_ENDPOINT_FILE", "BROWSER_CDP_WS_ENDPOINT_FILE"),
    )
    takeover_url: str = Field(
        "http://localhost:6080/vnc.html?autoconnect=true&resize=scale",
        alias="TAKEOVER_URL",
    )
    remote_access_info_path: str = Field(
        "/data/tunnels/reverse-ssh.json",
        alias="REMOTE_ACCESS_INFO_PATH",
    )
    remote_access_stale_after_seconds: float = Field(
        45.0,
        alias="REMOTE_ACCESS_STALE_AFTER_SECONDS",
    )
    artifact_root: str = Field("/data/artifacts", alias="ARTIFACT_ROOT")
    upload_root: str = Field("/data/uploads", alias="UPLOAD_ROOT")
    auth_root: str = Field("/data/auth", alias="AUTH_ROOT")
    approval_root: str = Field("/data/approvals", alias="APPROVAL_ROOT")
    audit_root: str = Field("/data/audit", alias="AUDIT_ROOT")
    witness_root: str = Field("/data/witness", alias="WITNESS_ROOT")
    state_db_path: str | None = Field(None, alias="STATE_DB_PATH")
    audit_max_events: int = Field(10000, alias="AUDIT_MAX_EVENTS")
    session_store_root: str = Field("/data/sessions", alias="SESSION_STORE_ROOT")
    job_store_root: str = Field("/data/jobs", alias="JOB_STORE_ROOT")
    redis_url: str | None = Field(None, alias="REDIS_URL")
    session_store_redis_prefix: str = Field(
        "auto_browser:sessions",
        alias="SESSION_STORE_REDIS_PREFIX",
    )
    agent_job_worker_count: int = Field(1, alias="AGENT_JOB_WORKER_COUNT")
    auth_state_encryption_key: str | None = Field(None, alias="AUTH_STATE_ENCRYPTION_KEY")
    require_auth_state_encryption: bool = Field(False, alias="REQUIRE_AUTH_STATE_ENCRYPTION")
    auth_state_max_age_hours: float = Field(72.0, alias="AUTH_STATE_MAX_AGE_HOURS")
    ocr_enabled: bool = Field(True, alias="OCR_ENABLED")
    ocr_language: str = Field("eng", alias="OCR_LANGUAGE")
    ocr_max_blocks: int = Field(20, alias="OCR_MAX_BLOCKS")
    ocr_text_limit: int = Field(1200, alias="OCR_TEXT_LIMIT")
    operator_id_header: str = Field("X-Operator-Id", alias="OPERATOR_ID_HEADER")
    operator_name_header: str = Field("X-Operator-Name", alias="OPERATOR_NAME_HEADER")
    require_operator_id: bool = Field(False, alias="REQUIRE_OPERATOR_ID")
    mcp_allowed_origins: str = Field("", alias="MCP_ALLOWED_ORIGINS")
    mcp_tool_profile: str = Field("curated", alias="MCP_TOOL_PROFILE")
    metrics_enabled: bool = Field(True, alias="METRICS_ENABLED")
    session_isolation_mode: str = Field("shared_browser_node", alias="SESSION_ISOLATION_MODE")
    isolated_browser_image: str = Field(
        "auto-browser-browser-node:latest",
        alias="ISOLATED_BROWSER_IMAGE",
    )
    isolated_browser_container_prefix: str = Field(
        "browser-session",
        alias="ISOLATED_BROWSER_CONTAINER_PREFIX",
    )
    isolated_browser_wait_timeout_seconds: int = Field(
        45,
        alias="ISOLATED_BROWSER_WAIT_TIMEOUT_SECONDS",
    )
    isolated_browser_keep_containers: bool = Field(
        False,
        alias="ISOLATED_BROWSER_KEEP_CONTAINERS",
    )
    isolated_browser_bind_host: str = Field("127.0.0.1", alias="ISOLATED_BROWSER_BIND_HOST")
    isolated_takeover_host: str = Field("127.0.0.1", alias="ISOLATED_TAKEOVER_HOST")
    isolated_takeover_scheme: str = Field("http", alias="ISOLATED_TAKEOVER_SCHEME")
    isolated_takeover_path: str = Field(
        "/vnc.html?autoconnect=true&resize=scale",
        alias="ISOLATED_TAKEOVER_PATH",
    )
    isolated_browser_network: str | None = Field(None, alias="ISOLATED_BROWSER_NETWORK")
    isolated_host_data_root: str | None = Field(None, alias="ISOLATED_HOST_DATA_ROOT")
    isolated_docker_host: str | None = Field(None, alias="ISOLATED_DOCKER_HOST")
    isolated_tunnel_enabled: bool = Field(False, alias="ISOLATED_TUNNEL_ENABLED")
    isolated_tunnel_host: str | None = Field(
        None,
        validation_alias=AliasChoices("ISOLATED_TUNNEL_HOST", "REVERSE_SSH_HOST"),
    )
    isolated_tunnel_port: int = Field(
        22,
        validation_alias=AliasChoices("ISOLATED_TUNNEL_PORT", "REVERSE_SSH_PORT"),
    )
    isolated_tunnel_user: str | None = Field(
        None,
        validation_alias=AliasChoices("ISOLATED_TUNNEL_USER", "REVERSE_SSH_USER"),
    )
    isolated_tunnel_key_path: str = Field("/data/ssh/id_ed25519", alias="ISOLATED_TUNNEL_KEY_PATH")
    isolated_tunnel_known_hosts_path: str = Field(
        "/data/ssh/known_hosts",
        alias="ISOLATED_TUNNEL_KNOWN_HOSTS_PATH",
    )
    isolated_tunnel_strict_host_key_checking: str = Field(
        "yes",
        alias="ISOLATED_TUNNEL_STRICT_HOST_KEY_CHECKING",
    )
    isolated_tunnel_remote_bind_address: str = Field(
        "127.0.0.1",
        alias="ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS",
    )
    isolated_tunnel_remote_port_start: int = Field(16181, alias="ISOLATED_TUNNEL_REMOTE_PORT_START")
    isolated_tunnel_remote_port_end: int = Field(16240, alias="ISOLATED_TUNNEL_REMOTE_PORT_END")
    isolated_tunnel_server_alive_interval: int = Field(
        30,
        alias="ISOLATED_TUNNEL_SERVER_ALIVE_INTERVAL",
    )
    isolated_tunnel_server_alive_count_max: int = Field(
        3,
        alias="ISOLATED_TUNNEL_SERVER_ALIVE_COUNT_MAX",
    )
    isolated_tunnel_info_interval_seconds: float = Field(
        10.0,
        alias="ISOLATED_TUNNEL_INFO_INTERVAL_SECONDS",
    )
    isolated_tunnel_startup_grace_seconds: float = Field(
        1.0,
        alias="ISOLATED_TUNNEL_STARTUP_GRACE_SECONDS",
    )
    isolated_tunnel_access_mode: str = Field(
        "private",
        validation_alias=AliasChoices("ISOLATED_TUNNEL_ACCESS_MODE", "REVERSE_SSH_ACCESS_MODE"),
    )
    isolated_tunnel_public_host: str | None = Field(
        None,
        validation_alias=AliasChoices("ISOLATED_TUNNEL_PUBLIC_HOST", "REVERSE_SSH_PUBLIC_HOST"),
    )
    isolated_tunnel_public_scheme: str = Field(
        "http",
        validation_alias=AliasChoices("ISOLATED_TUNNEL_PUBLIC_SCHEME", "REVERSE_SSH_PUBLIC_SCHEME"),
    )
    isolated_tunnel_local_host: str = Field("host.docker.internal", alias="ISOLATED_TUNNEL_LOCAL_HOST")
    isolated_tunnel_info_root: str = Field("/data/tunnels/sessions", alias="ISOLATED_TUNNEL_INFO_ROOT")
    allowed_hosts: str = Field("example.com,localhost", alias="ALLOWED_HOSTS")
    default_viewport_width: int = Field(1280, alias="DEFAULT_VIEWPORT_WIDTH")
    default_viewport_height: int = Field(800, alias="DEFAULT_VIEWPORT_HEIGHT")
    connect_retries: int = Field(60, alias="CONNECT_RETRIES")
    connect_retry_delay_seconds: float = Field(1.0, alias="CONNECT_RETRY_DELAY_SECONDS")
    max_sessions: int = Field(1, alias="MAX_SESSIONS")
    require_approval_for_uploads: bool = Field(True, alias="REQUIRE_APPROVAL_FOR_UPLOADS")
    approval_ttl_minutes: int = Field(15, alias="APPROVAL_TTL_MINUTES")
    witness_enabled: bool = Field(True, alias="WITNESS_ENABLED")
    witness_protection_mode_default: Literal["normal", "confidential"] = Field(
        "normal",
        alias="WITNESS_PROTECTION_MODE_DEFAULT",
    )
    witness_remote_url: str | None = Field(None, alias="WITNESS_REMOTE_URL")
    witness_remote_api_key: str | None = Field(None, alias="WITNESS_REMOTE_API_KEY")
    witness_remote_tenant_id: str | None = Field(None, alias="WITNESS_REMOTE_TENANT_ID")
    witness_remote_timeout_seconds: float = Field(0.75, alias="WITNESS_REMOTE_TIMEOUT_SECONDS")
    witness_remote_verify_tls: bool = Field(True, alias="WITNESS_REMOTE_VERIFY_TLS")
    witness_remote_required_for_confidential: bool = Field(
        False,
        alias="WITNESS_REMOTE_REQUIRED_FOR_CONFIDENTIAL",
    )
    enable_tracing: bool = Field(True, alias="ENABLE_TRACING")
    typing_delay_ms: int = Field(20, alias="TYPING_DELAY_MS")
    action_timeout_ms: int = Field(15000, alias="ACTION_TIMEOUT_MS")
    request_rate_limit_enabled: bool = Field(True, alias="REQUEST_RATE_LIMIT_ENABLED")
    request_rate_limit_requests: int = Field(120, alias="REQUEST_RATE_LIMIT_REQUESTS")
    request_rate_limit_window_seconds: int = Field(60, alias="REQUEST_RATE_LIMIT_WINDOW_SECONDS")
    request_rate_limit_exempt_paths: str = Field(
        "/healthz,/readyz,/docs,/openapi.json,/redoc,/artifacts,/metrics",
        alias="REQUEST_RATE_LIMIT_EXEMPT_PATHS",
    )
    mcp_session_store_path: str = Field("/data/mcp/sessions.json", alias="MCP_SESSION_STORE_PATH")
    cleanup_on_startup: bool = Field(True, alias="CLEANUP_ON_STARTUP")
    cleanup_interval_seconds: float = Field(3600.0, alias="CLEANUP_INTERVAL_SECONDS")
    artifact_retention_hours: float = Field(168.0, alias="ARTIFACT_RETENTION_HOURS")
    upload_retention_hours: float = Field(168.0, alias="UPLOAD_RETENTION_HOURS")
    auth_retention_hours: float = Field(168.0, alias="AUTH_RETENTION_HOURS")

    openai_api_key: str | None = Field(None, alias="OPENAI_API_KEY")
    openai_base_url: str = Field("https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    openai_model: str = Field("gpt-4.1-mini", alias="OPENAI_MODEL")
    openai_auth_mode: str = Field("api", alias="OPENAI_AUTH_MODE")
    openai_cli_path: str = Field("codex", alias="OPENAI_CLI_PATH")
    openai_cli_model: str | None = Field(None, alias="OPENAI_CLI_MODEL")
    openai_host_bridge_socket: str = Field(
        "/data/host-bridge/codex.sock",
        alias="OPENAI_HOST_BRIDGE_SOCKET",
    )

    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")
    anthropic_base_url: str = Field("https://api.anthropic.com/v1", alias="ANTHROPIC_BASE_URL")
    anthropic_version: str = Field("2023-06-01", alias="ANTHROPIC_VERSION")
    claude_model: str = Field("claude-sonnet-4-20250514", alias="CLAUDE_MODEL")
    claude_auth_mode: str = Field("api", alias="CLAUDE_AUTH_MODE")
    claude_cli_path: str = Field("claude", alias="CLAUDE_CLI_PATH")
    claude_cli_model: str | None = Field(None, alias="CLAUDE_CLI_MODEL")

    gemini_api_key: str | None = Field(None, alias="GEMINI_API_KEY")
    gemini_base_url: str = Field(
        "https://generativelanguage.googleapis.com/v1beta",
        alias="GEMINI_BASE_URL",
    )
    gemini_model: str = Field("gemini-2.5-flash", alias="GEMINI_MODEL")
    gemini_auth_mode: str = Field("api", alias="GEMINI_AUTH_MODE")
    gemini_cli_path: str = Field("gemini", alias="GEMINI_CLI_PATH")
    gemini_cli_model: str | None = Field(None, alias="GEMINI_CLI_MODEL")
    cli_home: str | None = Field("/data/cli-home", alias="CLI_HOME")

    model_request_timeout_seconds: float = Field(60.0, alias="MODEL_REQUEST_TIMEOUT_SECONDS")
    model_max_retries: int = Field(2, alias="MODEL_MAX_RETRIES")
    model_retry_backoff_seconds: float = Field(1.0, alias="MODEL_RETRY_BACKOFF_SECONDS")

    # Interaction pacing
    human_typing_min_delay_ms: int = Field(40, alias="HUMAN_TYPING_MIN_DELAY_MS")
    human_typing_max_delay_ms: int = Field(130, alias="HUMAN_TYPING_MAX_DELAY_MS")

    # Stealth / anti-bot
    stealth_enabled: bool = Field(True, alias="STEALTH_ENABLED")
    user_agent_pool: str = Field(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36,"
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36,"
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36,"
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        alias="USER_AGENT_POOL",
    )

    # Approval webhooks — notified when approvals are created or decided
    approval_webhook_url: str | None = Field(None, alias="APPROVAL_WEBHOOK_URL")
    approval_webhook_secret: str | None = Field(None, alias="APPROVAL_WEBHOOK_SECRET")

    # Perception presets — default mode for /observe
    # fast: screenshot only (skip OCR and accessibility tree)
    # normal: screenshot + OCR + accessibility tree (current default)
    # rich: normal + extended text + full DOM outline
    perception_preset_default: str = Field("normal", alias="PERCEPTION_PRESET_DEFAULT")

    # SSE keepalive — how often to send a comment to prevent proxy timeouts
    sse_keepalive_seconds: float = Field(15.0, alias="SSE_KEEPALIVE_SECONDS")

    # Proxy (session-level override supported in CreateSessionRequest)
    default_proxy_server: str | None = Field(None, alias="DEFAULT_PROXY_SERVER")
    default_proxy_username: str | None = Field(None, alias="DEFAULT_PROXY_USERNAME")
    default_proxy_password: str | None = Field(None, alias="DEFAULT_PROXY_PASSWORD")

    # Proxy personas — path to JSON file mapping name → {server, username, password}
    proxy_persona_file: str | None = Field(None, alias="PROXY_PERSONA_FILE")

    # PII scrubbing
    pii_scrub_enabled: bool = Field(True, alias="PII_SCRUB_ENABLED")
    pii_scrub_screenshot: bool = Field(True, alias="PII_SCRUB_SCREENSHOT")
    pii_scrub_network: bool = Field(True, alias="PII_SCRUB_NETWORK")
    pii_scrub_console: bool = Field(True, alias="PII_SCRUB_CONSOLE")
    pii_scrub_patterns: str = Field("", alias="PII_SCRUB_PATTERNS")  # "" = all patterns
    pii_scrub_replacement: str = Field("[REDACTED]", alias="PII_SCRUB_REPLACEMENT")
    pii_scrub_audit_report: bool = Field(True, alias="PII_SCRUB_AUDIT_REPORT")

    # Network inspector
    network_inspector_enabled: bool = Field(True, alias="NETWORK_INSPECTOR_ENABLED")
    network_inspector_max_entries: int = Field(500, alias="NETWORK_INSPECTOR_MAX_ENTRIES")
    network_inspector_capture_bodies: bool = Field(True, alias="NETWORK_INSPECTOR_CAPTURE_BODIES")
    network_inspector_body_max_bytes: int = Field(16384, alias="NETWORK_INSPECTOR_BODY_MAX_BYTES")

    # CDP attach mode — connect to an already-running Chrome instance
    cdp_connect_url: str | None = Field(None, alias="CDP_CONNECT_URL")

    # Shadow browsing — enable headed mode for debugging
    shadow_browse_enabled: bool = Field(True, alias="SHADOW_BROWSE_ENABLED")

    # Cron / webhook triggers
    cron_store_path: str = Field("/data/crons/crons.json", alias="CRON_STORE_PATH")
    cron_max_jobs: int = Field(50, alias="CRON_MAX_JOBS")

    # Shared session links
    share_token_secret: str | None = Field(None, alias="SHARE_TOKEN_SECRET")
    share_token_ttl_minutes: int = Field(60, alias="SHARE_TOKEN_TTL_MINUTES")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def allowed_host_patterns(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]

    @property
    def mcp_allowed_origin_list(self) -> list[str]:
        return [item.strip() for item in self.mcp_allowed_origins.split(",") if item.strip()]

    @property
    def request_rate_limit_exempt_path_list(self) -> list[str]:
        return [item.strip() for item in self.request_rate_limit_exempt_paths.split(",") if item.strip()]

    @property
    def environment_name(self) -> str:
        return self.app_env.strip().lower()

    @property
    def is_production(self) -> bool:
        return self.environment_name == "production"


    @property
    def random_user_agent(self) -> str:
        agents = [a.strip() for a in self.user_agent_pool.split(",") if a.strip()]
        return random.choice(agents) if agents else ""

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
